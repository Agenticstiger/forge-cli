# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An exhaustive audit of what a history's tracked categorical column lets a reader count.

A history writes, for each categorical column it tracks, ``initial`` (the level of each entity's
first version) and ``transitions`` (the changes between consecutive versions, grouped by the level
changed from). Beside the column's own published level histogram and the table's row count, a
reader can form from them every count of the column's *version table*: the versions at a level by
what came before them or what follows them, the unchanged pairs, the entities that last end at a
level, the changes into and out of a level, and so on. The profile therefore writes the two members
only when every count of that table passes the output check (:func:`fluid_build.profiler.history.
_checked_entries`), and writes both withheld whole otherwise.

This audit enumerates every world of a small universe (every multiset of member chains over a
column's levels, chains of at most ``maxlen`` versions, at most ``rows`` rows), profiles each
through the real profiler once, and groups the worlds by the document they publish: the worlds a
reader holding that document cannot tell apart. A count is *pinned* when every world of its group
gives it one value. The audit fails on a count that the rule protects (fewer than ``minimumCell``
records, or none where zeros are disclosive) that the document pins and the table's history-less
document (the same document with the history removed) does not.

Every baseline is a transform of the one document each world produces:

* ``H0``: the document without the table's ``history``;
* ``H1``: the document with the history's ``columns`` and ``reasons`` removed.

The counts judged, per tracked column, are the rows, first, last, stay, out, in and later counts at
each level, the change from each level to each other, the pair sums, and the totals (the unchanged
pairs, the changes, the entities that move, and the changes from a NULL where the column is
nullable); with two tracked columns also the pairs where both move, where none moves and where one
does, and the joint cells. A group is judged only where the universe holds all of it: every world
of it has at most ``rows - rounding base`` rows. A universe may fix its rows (``only_rows``), valid
at rounding base 1, where the published row count is exact.

A column that a declared ``endMarker`` or ``currentFlag`` names holds its marked value on a last
version alone; the profile withholds such a column whole, decided by the plan. Those universes
(``marker``) enumerate only the worlds that keep to the declaration.

Each universe also names the mutants that must pin a count, so the audit is known to see what it is
for: the per-cell publication the profile had before (``per_cell``), the check beside one of the
two directions alone (``one_direction``), no NULL gate (``no_null_gate``), a NULL gate that takes
the contract's word that a column is ``required`` over the records (``trusts_required``), the joint
check applied to a column a marker names (``joint_on_marker``), that check with the group a
marker empties left out of it (``exempt_structural``), and the decision that matches a marker's
value to a level by its spelling (``spelled_marker``): ``2.0`` names no level ``'2'`` there.

The default run holds two universes (about half a minute); the rest take longer, run with::

    FLUID_PROFILE_ORACLE=full pytest tests/profiler/test_history_column_oracle.py -n 3

CI runs them on a pull request that touches the profiler's history, references, runner or
disclosure modules or this file (``.github/workflows/profile-oracle.yml``), and on request.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

import pytest

from fluid_build.profiler import disclosure, history, runner, sources
from fluid_build.profiler._stats import load_backend

pytestmark = [pytest.mark.unit, pytest.mark.slow]

pytest.importorskip("flux_stats.fidelity")

#: A version's values: one level (or ``None``) for a single tracked column, a tuple for two.
Value = Any
World = Tuple[Tuple[Value, ...], ...]
Query = Tuple[str, Callable[[World], int]]

FULL = os.environ.get("FLUID_PROFILE_ORACLE") == "full"
needs_full = pytest.mark.skipif(not FULL, reason="set FLUID_PROFILE_ORACLE=full to run it")

TABLE = "card"
SECOND = ("X", "Y")


@dataclass(frozen=True)
class Universe:
    name: str
    levels: Tuple[str, ...]
    maxlen: int
    rows: int
    minimum_cell: int
    rounding: int
    zeros: bool
    #: The column may hold NULL in the records.
    nulls: bool = False
    #: The contract declares the column ``required`` whatever the records hold.
    required: bool = False
    #: A second tracked column, on these levels: worlds are chains of pairs.
    second: Tuple[str, ...] = ()
    #: Only the worlds with exactly this many rows (valid at rounding base 1).
    only_rows: Optional[int] = None
    #: The contract declares the column's last level a lifecycle value (``endMarker`` or
    #: ``currentFlag``): it is on a last version alone, and only the worlds that keep to that are
    #: enumerated.
    marker: str = ""
    #: The value the marker is written with, where it is not the last level as it stands: the
    #: same value spelled another way (``2.0`` for the level ``"2"``).
    marker_value: Any = None
    #: The fewest worlds in which a tracked column publishes its ``initial``.
    published: int = 1
    #: The mutants that must pin a count in this universe.
    mutants: Tuple[str, ...] = ()


D1 = Universe("d1", ("A", "B"), 3, 5, 2, 1, True, published=0, mutants=("per_cell",))
D2 = Universe("d2", ("A", "B"), 2, 7, 3, 1, False, published=15, mutants=("per_cell",))
F1 = Universe("f1", ("A", "B"), 3, 8, 3, 1, False, published=40, mutants=("one_direction",))
F2 = Universe("f2", ("A", "B", "C"), 2, 7, 3, 1, False, published=30)
F3 = Universe("f3", ("A", "B"), 3, 13, 2, 1, True, only_rows=12, published=5)
F4 = Universe(
    "f4", ("A", "B"), 2, 7, 3, 1, False, nulls=True, published=15, mutants=("no_null_gate",)
)
F5 = Universe("f5", ("A", "B"), 2, 7, 3, 1, False, second=SECOND, only_rows=6, published=100)
M1 = Universe(
    "m1",
    ("A", "B"),
    3,
    13,
    3,
    1,
    False,
    only_rows=12,
    published=0,
    marker="endMarker",
    mutants=("joint_on_marker",),
)
M2 = Universe(
    "m2",
    ("A", "B"),
    3,
    13,
    2,
    1,
    True,
    only_rows=12,
    published=0,
    marker="endMarker",
    mutants=("exempt_structural",),
)
M3 = replace(M1, name="m3", marker="currentFlag")
M4 = replace(
    M1,
    name="m4",
    levels=("1", "2"),
    marker_value=2.0,
    mutants=("spelled_marker",),
)
F6 = Universe(
    "f6",
    ("A", "B"),
    2,
    7,
    3,
    1,
    False,
    nulls=True,
    required=True,
    published=15,
    mutants=("trusts_required",),
)


def _worlds(universe: Universe) -> List[World]:
    """Every multiset of member chains: chains of at most ``maxlen`` versions, ``rows`` rows."""
    letters: List[Value] = list(universe.levels) + ([None] if universe.nulls else [])
    alphabet: List[Value] = (
        list(itertools.product(universe.levels, universe.second)) if universe.second else letters
    )
    types = [
        each
        for size in range(1, universe.maxlen + 1)
        for each in itertools.product(alphabet, repeat=size)
    ]
    found: List[World] = []

    def extend(at: int, left: int, held: Tuple[Tuple[Value, ...], ...]) -> None:
        if at == len(types):
            if universe.only_rows is None or left == universe.rows - universe.only_rows:
                found.append(held)
            return
        for count in range(left // len(types[at]) + 1):
            extend(at + 1, left - count * len(types[at]), held + (types[at],) * count)

    extend(0, universe.rows, ())
    if universe.marker:
        marked = universe.levels[-1]  # on a last version alone, as the contract declares
        found = [world for world in found if all(marked not in chain[:-1] for chain in world)]
    return found


def _enum(levels: Sequence[str]) -> str:
    return "enum(" + ",".join(f"'{each}'" for each in levels) + ")"


def _contract(universe: Universe) -> Dict[str, Any]:
    schema: List[Dict[str, Any]] = [
        {"name": "member_id", "type": "integer", "required": True},
        {"name": "valid_from", "type": "date", "required": True},
        {"name": "valid_to", "type": "date"},
        {
            "name": "tier",
            "type": _enum(universe.levels),
            **({"required": True} if universe.required or not universe.nulls else {}),
        },
    ]
    if universe.second:
        schema.append({"name": "plan", "type": _enum(universe.second), "required": True})
    entity: Dict[str, Any] = {"name": "member", "type": "natural", "expr": "member_id"}
    if universe.marker:
        entity["lifecycle"] = {
            universe.marker: {
                "column": "tier",
                "value": (
                    universe.levels[-1] if universe.marker_value is None else universe.marker_value
                ),
            },
        }
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "library",
        "metadata": {"layer": "Silver", "owner": {"team": "library"}},
        "exposes": [
            {
                "exposeId": TABLE,
                "kind": "table",
                "semantics": {
                    "entities": [entity],
                    "dimensions": [
                        {
                            "name": "valid_from",
                            "type": "time",
                            "typeParams": {
                                "timeGranularity": "day",
                                "validityParams": {"isStart": True},
                            },
                        },
                        {
                            "name": "valid_to",
                            "type": "time",
                            "typeParams": {
                                "timeGranularity": "day",
                                "validityParams": {"isEnd": True, "chain": "nextStart"},
                            },
                        },
                    ],
                },
                "contract": {"schema": schema},
            }
        ],
    }


class _Audit:
    """The worlds of a universe, each profiled once through the real profiler."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, universe: Universe) -> None:
        self.universe = universe
        self.rule = disclosure.DisclosureRule(
            universe.name,
            universe.minimum_cell,
            universe.zeros,
            universe.rounding,
            2,
            "0.90",
            "0.10",
        )
        self.backend = load_backend()
        self.contract = _contract(universe)
        self.current: Dict[str, World] = {}
        monkeypatch.setattr(sources, "read_columns", self._read)

    def _read(
        self, source: str, names: Sequence[str], *, table: Any = None, cwd: Any = None
    ) -> Any:
        rows: List[Dict[str, Any]] = []
        for member, chain in enumerate(self.current["world"]):
            begins = date(2020, 1, 1) + timedelta(days=member % 90)
            for place, held in enumerate(chain):
                last = place == len(chain) - 1
                tier, plan = held if self.universe.second else (held, "X")
                rows.append(
                    {
                        "member_id": member,
                        "valid_from": begins.isoformat(),
                        "valid_to": None if last else (begins + timedelta(days=30)).isoformat(),
                        "tier": tier,
                        "plan": plan,
                    }
                )
                begins += timedelta(days=30)
        return sources.SourceColumns(
            TABLE, len(rows), {name: [row[name] for row in rows] for name in names}
        )

    def profile(self, world: World) -> Dict[str, Any]:
        self.current["world"] = world
        result = runner.profile_exposes(
            self.contract,
            [runner.ExposeSource(TABLE, TABLE)],
            backend=self.backend,
            rule=self.rule,
        )
        document: Dict[str, Any] = json.loads(result.canonical)
        return document


# -- the counts a reader can form -----------------------------------------------------------------


def _pairs(chain: Sequence[Any]) -> Sequence[Tuple[Any, Any]]:
    return list(zip(chain, chain[1:], strict=False))


def _column_queries(
    prefix: str, levels: Sequence[str], nulls: bool, project: Callable[[World], List[List[Any]]]
) -> List[Query]:
    """Every count of one tracked column's version table that a world has, by name."""
    found: List[Query] = []

    def add(name: str, count: Callable[[List[List[Any]]], int]) -> None:
        found.append((prefix + name, lambda w, count=count: count(project(w))))

    for k in levels:
        add(f"rows@{k}", lambda p, k=k: sum(c.count(k) for c in p))
        add(f"first@{k}", lambda p, k=k: sum(1 for c in p if c[0] == k))
        add(f"last@{k}", lambda p, k=k: sum(1 for c in p if c[-1] == k))
        add(f"stay@{k}", lambda p, k=k: sum(1 for c in p for a, b in _pairs(c) if a == b == k))
        add(
            f"out@{k}",
            lambda p, k=k: sum(
                1 for c in p for a, b in _pairs(c) if a == k and b is not None and b != k
            ),
        )
        add(f"in@{k}", lambda p, k=k: sum(1 for c in p for a, b in _pairs(c) if b == k and a != k))
        add(f"later@{k}", lambda p, k=k: sum(c[1:].count(k) for c in p))
        for other in levels:
            if other != k:
                add(
                    f"{k}->{other}",
                    lambda p, k=k, o=other: sum(
                        1 for c in p for a, b in _pairs(c) if (a, b) == (k, o)
                    ),
                )
    for first, second in itertools.combinations(levels, 2):
        add(
            f"rows@{first}+{second}",
            lambda p, a=first, b=second: sum(c.count(a) + c.count(b) for c in p),
        )
        add(
            f"first@{first}+{second}",
            lambda p, a=first, b=second: sum(1 for c in p if c[0] in (a, b)),
        )
        add(
            f"{first}<->{second}",
            lambda p, a=first, b=second: sum(
                1 for c in p for x, y in _pairs(c) if {x, y} == {a, b}
            ),
        )
    add("changes", lambda p: sum(1 for c in p for a, b in _pairs(c) if a != b and b is not None))
    add(
        "movers", lambda p: sum(1 for c in p if any(a != b and b is not None for a, b in _pairs(c)))
    )
    add(
        "unchanged",
        lambda p: sum(1 for c in p for a, b in _pairs(c) if not (b is not None and a != b)),
    )
    add("stays", lambda p: sum(1 for c in p for a, b in _pairs(c) if a == b and a is not None))
    if nulls:
        add(
            "fromNull",
            lambda p: sum(1 for c in p for a, b in _pairs(c) if a is None and b is not None),
        )
    return found


def _queries(universe: Universe) -> List[Query]:
    if not universe.second:
        return _column_queries(
            "", universe.levels, universe.nulls, lambda w: [list(chain) for chain in w]
        )

    def part(at: int) -> Callable[[World], List[List[Any]]]:
        return lambda w: [[held[at] for held in chain] for chain in w]

    found = _column_queries("tier.", universe.levels, False, part(0))
    found += _column_queries("plan.", universe.second, False, part(1))
    found.append(
        (
            "both",
            lambda w: sum(1 for c in w for a, b in _pairs(c) if a[0] != b[0] and a[1] != b[1]),
        )
    )
    found.append(("none", lambda w: sum(1 for c in w for a, b in _pairs(c) if a == b)))
    found.append(
        (
            "only.tier",
            lambda w: sum(1 for c in w for a, b in _pairs(c) if a[0] != b[0] and a[1] == b[1]),
        )
    )
    found.append(
        (
            "only.plan",
            lambda w: sum(1 for c in w for a, b in _pairs(c) if a[0] == b[0] and a[1] != b[1]),
        )
    )
    for k in universe.levels:
        for x in universe.second:
            found.append((f"cell@{k}{x}", lambda w, k=k, x=x: sum(c.count((k, x)) for c in w)))
    return found


# -- the documents ----------------------------------------------------------------------------------


def _digest(document: Dict[str, Any]) -> bytes:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).digest()


def _variants(document: Dict[str, Any]) -> Dict[str, bytes]:
    """The document's identity, and the identity of its baselines (a transform of it each)."""
    shown = _digest(document)
    bare = copy.deepcopy(document)
    bare["tables"][TABLE].pop("history", None)
    thin = copy.deepcopy(document)
    held = thin["tables"][TABLE].get("history")
    if held is not None:
        held.pop("columns", None)
        held.pop("reasons", None)
    return {"full": shown, "H0": _digest(bare), "H1": _digest(thin)}


def _published(document: Dict[str, Any]) -> int:
    """Whether a tracked column of the table's history publishes any group of its ``initial``."""
    columns = document["tables"][TABLE].get("history", {}).get("columns", {})
    return int(
        any(
            group["status"] == "F"
            for column in columns.values()
            for group in column["initial"]["groups"]
        )
    )


def _pins(
    worlds: Sequence[World],
    shown: Dict[World, bytes],
    baseline: Dict[World, bytes],
    queries: Sequence[Query],
    values: Dict[World, Tuple[int, ...]],
    rule: Any,
    rows: int,
) -> List[str]:
    """Each protected count the documents in *shown* pin that those in *baseline* do not."""
    by_shown: Dict[bytes, List[World]] = defaultdict(list)
    by_baseline: Dict[bytes, List[World]] = defaultdict(list)
    for world in worlds:
        by_shown[shown[world]].append(world)
        by_baseline[baseline[world]].append(world)
    held: Dict[Tuple[bytes, int], bool] = {}

    def baseline_pins(key: bytes, at: int) -> bool:
        if (key, at) not in held:
            held[(key, at)] = len({values[w][at] for w in by_baseline[key]}) == 1
        return held[(key, at)]

    found: List[str] = []
    for group in by_shown.values():
        if any(sum(len(chain) for chain in world) > rows - rule.rounding_base for world in group):
            continue  # a larger world, outside the universe, may publish this document too
        for at, (name, _) in enumerate(queries):
            seen = {values[world][at] for world in group}
            if len(seen) != 1:
                continue
            (value,) = seen
            if not (value < rule.minimum_cell and (value > 0 or rule.zeros_disclosive)):
                continue  # not a count the rule protects
            if not all(baseline_pins(baseline[world], at) for world in group):
                found.append(f"{name} = {value} in worlds like {group[0]}")
    return found


# -- the mutants ------------------------------------------------------------------------------------


def _per_cell(
    context: Any, window: Any, column: Any, stations: Any
) -> Dict[str, Any]:  # pragma: no cover - the mutant
    """What a history wrote before: each member checked on its own, cell by cell."""
    return {
        "initial": history._initial(context, column, stations),
        "transitions": _exempt_transitions(context, window, column, stations),
    }


def _trusts_required(context: Any, column: Any) -> bool:  # pragma: no cover - the mutant
    """The NULL gate that takes the contract's ``required`` over what the records hold."""
    if history._required(context, column.name):
        return True
    table = context.stage.tables_doc[context.expose_id]
    count = table.get("columns", {}).get(column.name, {}).get("metrics", {}).get("count", {})
    return bool(
        count.get("status") == "F"
        and count.get("value") is not None
        and count.get("value") == table.get("rowCount")
    )


def _final_levels(window: Any, column: Any) -> FrozenSet[int]:
    """The levels of *column* a declared marker's value spells, as the mutants read a marker.

    A declared ``endMarker`` holds its value on the final version only, and a ``currentFlag`` on
    the last version only, so no change leaves that value. The profile does not read the value:
    the marker names the column. The mutants below match it by its spelling.
    """
    found: Set[int] = set()
    for declared in (window.end_marker, window.current_flag):
        if declared is None or declared[0] != column.name:
            continue
        value = declared[1]
        spelled = {str(value)} | (
            {"true" if value else "false"} if isinstance(value, bool) else set()
        )
        found.update(at for at, level in enumerate(column.levels) if level in spelled)
    return frozenset(found)


def _spelled_entries(
    context: Any, window: Any, column: Any, stations: Any
) -> Dict[str, Any]:  # pragma: no cover - the mutant
    """The decision that matches a marker's value to a level by its spelling."""
    if _final_levels(window, column):
        return history._withheld_entries(context, column)
    return history._checked_entries(context, window, column, stations)


def _exempt_passes(
    context: Any, window: Any, column: Any, stations: Any
) -> bool:  # pragma: no cover - the mutant
    """:func:`history._passes` with a level the contract empties left out of the check.

    The group of versions that follow a marked value is empty by the contract, and where the
    records leave it empty the mutant drops it from the check and lets a cell of none pass.
    """
    from fluid_build.profiler import dates

    if not history._holds_no_null(context, column):
        return False
    stage = context.stage
    formed = runner.column_edge_statistics(
        history._formed_block(context), context.read, stage.backend, column.name
    )
    if any(measured.left_out_unread for _, measured in formed):
        return False
    table = history._version_table(column, stations)
    final = {at for at in _final_levels(window, column) if sum(table.pairs[at]) == 0}
    (before, before_measured), (after, after_measured) = history._level_tables(
        context, column, table
    )
    dropped = {f"\x00before:{at}" for at in final}
    before_measured = replace(
        before_measured,
        groups=tuple(each for each in before_measured.groups if each.key not in dropped),
    )
    rule = stage.rule
    if final and rule.zeros_disclosive:
        rule = replace(rule, zeros_disclosive=False)
        cells: List[int] = []
        for each in before_measured.groups:
            cells += [each.k, *each.cells]
        for each in after_measured.groups:
            cells.append(each.k)
            end = each.key.startswith("\x00end")
            cells += [c for at, c in enumerate(each.cells) if end or at not in final]
        for left in (before_measured.left_out, after_measured.left_out):
            if left is not None:
                cells += list(left.cells)
        if 0 in cells:  # a cell of none beyond the emptied level still discloses
            return False
    total = runner._column_total(*formed[0]) if formed else table.records
    decisions = dates.checked(
        [*formed, (before, before_measured), (after, after_measured)],
        name="versions",
        total=total,
        rule=rule,
        backend=stage.backend,
        timed=context.timed,
        what="the history of",
    )
    written = runner._edge_decisions(formed, stage.backend, stage.rule)
    for at in range(len(formed)):
        now, then = decisions[at], written[at]
        if dict(now.group_status) != dict(then.group_status) or dict(now.cell_status) != dict(
            then.cell_status
        ):
            return False
    return all(
        status == disclosure.FREE
        for decision in decisions[len(formed) :]
        for status in (
            *decision.group_status.values(),
            *(s for each in decision.cell_status.values() for s in each),
        )
    )


def _exempt_transitions(
    context: Any, window: Any, column: Any, stations: Any
) -> Dict[str, Any]:  # pragma: no cover - the mutant
    """:func:`history._transitions` with the group a marker empties out of its check, as zeros."""
    backend = context.stage.backend
    levels, size = column.levels, len(column.levels)
    moves, from_null = history._move_counts(column, stations)
    total = from_null + sum(sum(each) for each in moves)
    name = f"{context.expose_id}.{column.name} transitions"
    slots = [str(place) for place in range(size - 1)]
    final = _final_levels(window, column)
    kept = {
        level: (
            []
            if at in final and not any(moves[at])
            else [place for place in range(size) if place != at]
        )
        for at, level in enumerate(levels)
    }
    seen = history.MeasuredStatistic(
        groups=tuple(
            backend.count_levels([moves[at][place] for place in kept[level]], slots, level)
            for at, level in enumerate(levels)
            if kept[level]
        ),
        left_out=(
            backend.count_levels([from_null, *([0] * (size - 2))], slots) if from_null else None
        ),
    )
    decision = context.check(
        history._level_statistic(name, slots, column.name), seen, name=name, total=total
    )
    statistic = history._level_statistic(name, levels, column.name)
    measured = history.MeasuredStatistic(
        groups=tuple(
            backend.count_levels(moves[at], levels, level) for at, level in enumerate(levels)
        )
    )
    return context.entry(statistic, measured, history._expanded(decision, kept, size))


def _exempt_entries(
    context: Any, window: Any, column: Any, stations: Any
) -> Dict[str, Any]:  # pragma: no cover - the mutant
    if _exempt_passes(context, window, column, stations):
        entries = {
            "initial": history._initial(context, column, stations),
            "transitions": _exempt_transitions(context, window, column, stations),
        }
        if all(history._written_in_full(entry) for entry in entries.values()):
            return entries
    return history._withheld_entries(context, column)


def _mutate(monkeypatch: pytest.MonkeyPatch, mutant: str) -> None:
    if mutant == "per_cell":
        monkeypatch.setattr(history, "_entries", _per_cell)
    elif mutant == "one_direction":
        both = history._level_tables
        monkeypatch.setattr(history, "_level_tables", lambda *args: both(*args)[:1])
    elif mutant == "no_null_gate":
        monkeypatch.setattr(history, "_holds_no_null", lambda context, column: True)
    elif mutant == "joint_on_marker":
        monkeypatch.setattr(history, "_entries", history._checked_entries)
    elif mutant == "spelled_marker":
        monkeypatch.setattr(history, "_entries", _spelled_entries)
    elif mutant == "exempt_structural":
        monkeypatch.setattr(history, "_entries", _exempt_entries)
    elif mutant == "trusts_required":
        monkeypatch.setattr(history, "_holds_no_null", _trusts_required)
    else:  # pragma: no cover - a typo in a universe
        raise AssertionError(mutant)


def _run(universe: Universe, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    audit = _Audit(monkeypatch, universe)
    worlds = _worlds(universe)
    queries = _queries(universe)
    values = {world: tuple(count(world) for _, count in queries) for world in worlds}

    def form() -> Tuple[Dict[str, Dict[World, bytes]], int]:
        kept: Dict[str, Dict[World, bytes]] = {"full": {}, "H0": {}, "H1": {}}
        published = 0
        for world in worlds:
            document = audit.profile(world)
            published += _published(document)
            for name, digest in _variants(document).items():
                kept[name][world] = digest
        return kept, published

    kept, published = form()
    pins = {
        f"{name} vs {base}": _pins(
            worlds, kept[name], kept[base], queries, values, audit.rule, universe.rows
        )
        for name, base in (("full", "H0"), ("full", "H1"), ("H1", "H0"))
    }
    mutants: Dict[str, List[str]] = {}
    for mutant in universe.mutants:
        with monkeypatch.context() as patched:
            _mutate(patched, mutant)
            changed, _ = form()
        mutants[mutant] = _pins(
            worlds, changed["full"], kept["H0"], queries, values, audit.rule, universe.rows
        )
    return {"worlds": len(worlds), "published": published, "pins": pins, "mutants": mutants}


def _audit_universe(universe: Universe, monkeypatch: pytest.MonkeyPatch) -> None:
    started = time.time()
    done = _run(universe, monkeypatch)
    assert done["worlds"] > 0
    for name, found in done["pins"].items():
        assert found == [], (name, found[:3])
    assert done["published"] >= universe.published, (done["published"], time.time() - started)
    print(  # the table of what each universe holds, in the report of a run with ``-rP``
        f"{universe.name}: {done['worlds']} worlds, {done['published']} publish, mutants pin "
        f"{ {mutant: len(done['mutants'][mutant]) for mutant in universe.mutants} }, "
        f"{time.time() - started:.0f}s"
    )
    for mutant in universe.mutants:
        assert done["mutants"][mutant], f"the {mutant} mutant gives no count away"


def test_d1_a_tracked_column_pins_no_count_when_zeros_are_disclosive(monkeypatch):
    _audit_universe(D1, monkeypatch)


def test_d2_a_tracked_column_pins_no_count_over_two_levels_of_chains_of_two(monkeypatch):
    _audit_universe(D2, monkeypatch)


@needs_full
@pytest.mark.timeout(1800)
def test_f1_a_tracked_column_pins_no_count_over_chains_of_three(monkeypatch):
    _audit_universe(F1, monkeypatch)


@needs_full
@pytest.mark.timeout(2400)
def test_f2_a_tracked_column_pins_no_count_over_three_levels(monkeypatch):
    _audit_universe(F2, monkeypatch)


@needs_full
@pytest.mark.timeout(3600)
def test_f3_a_tracked_column_pins_no_count_in_tables_of_twelve_rows(monkeypatch):
    _audit_universe(F3, monkeypatch)


@needs_full
@pytest.mark.timeout(2400)
def test_f4_a_nullable_tracked_column_pins_no_count(monkeypatch):
    _audit_universe(F4, monkeypatch)


@needs_full
@pytest.mark.timeout(2400)
def test_f6_a_required_column_the_records_leave_null_pins_no_count(monkeypatch):
    _audit_universe(F6, monkeypatch)


@needs_full
@pytest.mark.timeout(1800)
def test_m1_a_column_an_end_marker_names_pins_no_count(monkeypatch):
    _audit_universe(M1, monkeypatch)


@needs_full
@pytest.mark.timeout(1800)
def test_m2_a_column_an_end_marker_names_pins_no_count_where_zeros_are_disclosive(monkeypatch):
    _audit_universe(M2, monkeypatch)


@needs_full
@pytest.mark.timeout(1800)
def test_m3_a_column_a_current_flag_names_pins_no_count(monkeypatch):
    _audit_universe(M3, monkeypatch)


@needs_full
@pytest.mark.timeout(1800)
def test_m4_a_column_a_marker_names_pins_no_count_whatever_the_marker_s_spelling(monkeypatch):
    _audit_universe(M4, monkeypatch)


@needs_full
@pytest.mark.timeout(2400)
def test_f5_two_tracked_columns_pin_no_count_or_joint_count(monkeypatch):
    _audit_universe(F5, monkeypatch)
