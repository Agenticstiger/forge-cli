# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An exhaustive audit of a history that follows a foreign key into a public table.

A history that follows a key publishes its ``initial`` and ``transitions``; each published cell is a
floor on the children of one parent of the relationship the key belongs to, and that relationship
publishes its children per parent beside them. The profile therefore withholds the relationship's
children per parent whole wherever its key is followed, a decision of the plan alone
(:func:`fluid_build.profiler.history.followed_columns`): no count and no value is read to take it.
The key's two members are themselves written only where every count of the key's version table
passes the output check (:func:`fluid_build.profiler.history._entries`), and withheld whole
otherwise; ``tests/profiler/test_history_column_oracle.py`` audits that rule on plain columns.

This audit enumerates every world of a small universe (a table of keys and every multiset of
members, each with a chain of at most two branches), profiles each through the real profiler, and
groups the worlds by the document they publish: the worlds a reader holding that document cannot
tell apart. A count is *pinned* when every world of its group gives it one value. The audit fails
on a named count that the rule protects (fewer than ``minimumCell`` records, or none where zeros are
disclosive) that the document pins and the document of the same world that follows no key does not:

* ``rows@k``, ``first@k``, ``k->l``: the rows at a branch, the members that start there, and the
  moves from one branch to another, and each sum of two of them;
* ``last@k``, ``stay@k``, ``out@k``, ``in@k``, ``later@k``: the members that end at a branch, the
  consecutive versions that stay at it, leave it, enter it, and the versions after a first one;
  and ``stays``, ``changes`` and ``movers`` in total;
* ``bins[i..j]``: the branches whose rows lie in a run of the bins of the relationship's children
  per parent (the bins where the universe's edges put them).

A group is judged only where the universe holds all of it: every world of it has at most
``MAX_ROWS - rounding base`` rows. Where a mutant must pin a count, it does: the one that publishes
each cell of the key's two members as the profile once did (``per_cell``), in every universe that
names it.

The universes are small enough to enumerate. The default run holds two of them and the table of two
keys (O6): ``o0`` (four keys, members with one branch, at most five rows, about 126 worlds and 11
seconds), which must pin no named count or run of bins, and ``o8`` (three keys, chains of two, tables
of exactly four rows, 114 worlds), where the per-cell mutant must pin a named count. The full set
(O1 to O5, and O7, three keys with zeros not disclosive, where the key's members are published)
takes about an hour; O1, O2 and O7 carry the mutant as well. Run it with::

    FLUID_PROFILE_ORACLE=full pytest tests/profiler/test_reference_follow_oracle.py -n 3

CI runs it on a pull request that touches the profiler's history, references, runner or
disclosure modules or this file (``.github/workflows/profile-oracle.yml``), and on request.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler import disclosure, history, references, runner, sources
from fluid_build.profiler._stats import load_backend
from tests.profiler import _library as library

pytestmark = [pytest.mark.unit, pytest.mark.slow]

pytest.importorskip("flux_stats.fidelity")

World = Tuple[Tuple[str, ...], ...]
Query = Tuple[str, str, Callable[[World], int]]

FULL = os.environ.get("FLUID_PROFILE_ORACLE") == "full"
needs_full = pytest.mark.skipif(not FULL, reason="set FLUID_PROFILE_ORACLE=full to run it")


# A universe of the full set runs for minutes (one worker on a laptop took about 2, 5, 6, 9 and 4
# minutes for o1 to o5), and a shared runner is slower, so each carries a bound of its own in place
# of the repository's 10-minute default: that default ends the worker, and the run reports a crash.


@dataclass(frozen=True)
class Universe:
    name: str
    keys: int
    chains: int
    rows: int
    #: The relationship's bin edges: ``None`` for the production ones.
    edges: Tuple[int, ...]
    minimum_cell: int
    rounding: int
    #: Whether ``bins[...]`` counts are judged too, or the named ones alone.
    bins: bool
    #: The fewest worlds that must publish a cell of the followed key.
    published: int
    #: The mutants that must pin a named count: ``per_cell`` (each cell of the key's two members
    #: published on its own, as the profile once did).
    mutants: Tuple[str, ...]
    #: Whether a cell of no records is disclosive.
    zeros: bool = True
    #: Only the worlds with exactly this many rows (valid at rounding base 1, where the published
    #: row count is exact); ``rows`` is then one more.
    only_rows: Optional[int] = None


UNIVERSES = [
    Universe("o0", 4, 1, 5, (0, 1, 2), 2, 1, True, 0, ()),
    Universe("o1", 4, 2, 5, (0, 1, 2), 2, 1, True, 0, ("per_cell",)),
    Universe("o2", 4, 2, 6, (0, 1, 2), 2, 1, True, 0, ("per_cell",)),
    Universe("o3", 3, 3, 7, (0, 1, 2), 2, 1, False, 0, ()),
    Universe("o4", 4, 2, 7, (), 2, 1, False, 0, ()),
    Universe("o5", 3, 2, 8, (0, 1, 2, 3), 3, 2, False, 0, ()),
    Universe("o7", 3, 2, 7, (0, 1, 2), 3, 1, False, 30, ("per_cell",), zeros=False),
    Universe("o8", 3, 2, 5, (0, 1, 2), 2, 1, False, 0, ("per_cell",), only_rows=4),
]


def _worlds(
    keys: Sequence[str], chains: int, rows: int, only_rows: Optional[int] = None
) -> List[World]:
    """Every multiset of at most *rows* versions, held by members with chains up to *chains* long.

    With *only_rows*, only those of exactly that many versions."""
    types = [each for size in range(1, chains + 1) for each in itertools.product(keys, repeat=size)]
    found: List[World] = []

    def extend(at: int, left: int, held: Tuple[Tuple[str, ...], ...]) -> None:
        if at == len(types):
            if only_rows is None or left == rows - only_rows:
                found.append(held)
            return
        for count in range(left // len(types[at]) + 1):
            extend(at + 1, left - count * len(types[at]), held + (types[at],) * count)

    extend(0, rows, ())
    return found


def _queries(keys: Sequence[str], edges: Sequence[int]) -> List[Query]:
    """The named counts, and the number of branches in each run of bins, a world holds."""
    lower = list(edges[:-1])

    def bin_of(count: int) -> int:
        return min(bisect_right(edges, count) - 1, len(lower) - 1)

    found: List[Query] = []
    for key in keys:
        found.append(("named", f"rows@{key}", lambda w, k=key: sum(c.count(k) for c in w)))
        found.append(("named", f"first@{key}", lambda w, k=key: sum(1 for c in w if c[0] == k)))
        for other in keys:
            if other != key:
                found.append(
                    (
                        "named",
                        f"{key}->{other}",
                        lambda w, k=key, o=other: sum(
                            1 for c in w for a, b in zip(c, c[1:], strict=False) if (a, b) == (k, o)
                        ),
                    )
                )
    for key in keys:
        found.append(("named", f"last@{key}", lambda w, k=key: sum(1 for c in w if c[-1] == k)))
        found.append(
            (
                "named",
                f"stay@{key}",
                lambda w, k=key: sum(
                    1 for c in w for a, b in zip(c, c[1:], strict=False) if a == b == k
                ),
            )
        )
        found.append(
            (
                "named",
                f"out@{key}",
                lambda w, k=key: sum(
                    1 for c in w for a, b in zip(c, c[1:], strict=False) if a == k and b != k
                ),
            )
        )
        found.append(
            (
                "named",
                f"in@{key}",
                lambda w, k=key: sum(
                    1 for c in w for a, b in zip(c, c[1:], strict=False) if b == k and a != k
                ),
            )
        )
        found.append(("named", f"later@{key}", lambda w, k=key: sum(c[1:].count(k) for c in w)))
    found.append(
        (
            "named",
            "stays",
            lambda w: sum(1 for c in w for a, b in zip(c, c[1:], strict=False) if a == b),
        )
    )
    found.append(
        (
            "named",
            "changes",
            lambda w: sum(1 for c in w for a, b in zip(c, c[1:], strict=False) if a != b),
        )
    )
    found.append(
        (
            "named",
            "movers",
            lambda w: sum(1 for c in w if any(a != b for a, b in zip(c, c[1:], strict=False))),
        )
    )
    for first, second in itertools.combinations(keys, 2):
        found.append(
            (
                "named",
                f"rows@{first}+{second}",
                lambda w, a=first, b=second: sum(c.count(a) + c.count(b) for c in w),
            )
        )
        found.append(
            (
                "named",
                f"first@{first}+{second}",
                lambda w, a=first, b=second: sum(1 for c in w if c[0] in (a, b)),
            )
        )
    for low in range(len(lower)):
        for high in range(low, len(lower)):
            found.append(
                (
                    "bins",
                    f"bins[{low}..{high}]",
                    lambda w, i=low, j=high: sum(
                        1 for k in keys if i <= bin_of(sum(c.count(k) for c in w)) <= j
                    ),
                )
            )
    return found


def _contract() -> Dict[str, Any]:
    built = library.contract(key_type="string")
    built["metadata"] = {"layer": "Silver", "owner": {"team": "library"}}
    for expose in built["exposes"]:
        for column in expose["contract"]["schema"]:
            if column["name"] in ("member_id", "valid_from", "branch_id"):
                column["required"] = True
    return built


class _Audit:
    """The worlds of a universe, each profiled the way the change writes it and without it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, keys: Sequence[str], rule: Any) -> None:
        self.keys, self.rule, self.monkeypatch = list(keys), rule, monkeypatch
        self.backend = load_backend()
        self.contract = _contract()
        self.current: Dict[str, World] = {}
        monkeypatch.setattr(sources, "read_columns", self._read)

    def _read(
        self, source: str, names: Sequence[str], *, table: Any = None, cwd: Any = None
    ) -> Any:
        if source == "branch":
            values = {"branch_id": self.keys, "city": [f"town {key}" for key in self.keys]}
            return sources.SourceColumns("branch", len(self.keys), {n: values[n] for n in names})
        rows: List[Dict[str, Any]] = []
        for member, chain in enumerate(self.current["world"]):
            begins = date(2020, 1, 1) + timedelta(days=member % 90)
            for place, key in enumerate(chain):
                last = place == len(chain) - 1
                rows.append(
                    {
                        "member_id": member,
                        "valid_from": begins.isoformat(),
                        "valid_to": None if last else (begins + timedelta(days=30)).isoformat(),
                        "branch_id": key,
                    }
                )
                begins += timedelta(days=30)
        columns = {name: [row[name] for row in rows] for name in names}
        return sources.SourceColumns("card", len(rows), columns)

    def profile(self, world: World) -> Any:
        self.current["world"] = world
        return runner.profile_exposes(
            self.contract,
            [runner.ExposeSource("branch", "branch"), runner.ExposeSource("card", "card")],
            backend=self.backend,
            rule=self.rule,
        )


def _digest(result: Any) -> bytes:
    """The document's identity, kept small: a world holds only this, not the document."""
    return hashlib.sha256(result.canonical).digest()


def _published_cells(result: Any) -> int:
    """Whether the key's history publishes any cell of its ``initial`` or ``transitions``."""
    history = result.document["tables"]["card"].get("history", {})
    column = history.get("columns", {}).get("branch_id")
    if not column:
        return 0
    groups = [*column["initial"]["groups"], *column["transitions"]["groups"]]
    return int(any(group["status"] == "F" for group in groups))


def _pins(
    worlds: Sequence[World],
    shown: Dict[World, bytes],
    baseline: Dict[World, bytes],
    queries: Sequence[Query],
    rule: Any,
    rows: int,
) -> DefaultDict[str, List[str]]:
    """Each named or binned count the documents in *shown* pin that those in *baseline* do not."""
    by_shown: DefaultDict[bytes, List[World]] = defaultdict(list)
    by_baseline: DefaultDict[bytes, List[World]] = defaultdict(list)
    for world in worlds:
        by_shown[shown[world]].append(world)
        by_baseline[baseline[world]].append(world)
    found: DefaultDict[str, List[str]] = defaultdict(list)
    for group in by_shown.values():
        if any(sum(len(chain) for chain in world) > rows - rule.rounding_base for world in group):
            continue  # a larger world, outside the universe, may publish this document too
        for kind, name, count in queries:
            seen = {count(world) for world in group}
            if len(seen) != 1:
                continue
            (value,) = seen
            if not (value < rule.minimum_cell and (value > 0 or rule.zeros_disclosive)):
                continue  # not a count the rule protects
            if any(len({count(w) for w in by_baseline[baseline[x]]}) > 1 for x in group):
                found[kind].append(f"{name} = {value} in worlds like {group[0]}")
    return found


def _per_cell(context: Any, window: Any, column: Any, stations: Any) -> Dict[str, Any]:
    """What a history wrote of a column before: each member checked on its own, cell by cell."""
    return {
        "initial": history._initial(context, column, stations),
        "transitions": history._transitions(context, window, column, stations),
    }


def _run(universe: Universe, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    keys = [chr(ord("A") + at) for at in range(universe.keys)]
    if universe.edges:
        monkeypatch.setattr(runner, "CHILDREN_EDGES", (*universe.edges, disclosure.MAX_COUNT))
    rule = disclosure.DisclosureRule(
        universe.name,
        universe.minimum_cell,
        universe.zeros,
        universe.rounding,
        2,
        "0.90",
        "0.10",
    )
    audit = _Audit(monkeypatch, keys, rule)
    worlds = _worlds(keys, universe.chains, universe.rows, universe.only_rows)
    queries = [
        each
        for each in _queries(keys, runner.CHILDREN_EDGES)
        if universe.bins or each[0] == "named"
    ]
    shown: Dict[World, bytes] = {}
    published = 0
    for world in worlds:
        formed = audit.profile(world)
        shown[world] = _digest(formed)
        published += _published_cells(formed)
    with monkeypatch.context() as patched:
        patched.setattr(references.References, "follow", lambda self, child, column: None)
        baseline = {world: _digest(audit.profile(world)) for world in worlds}
    judged = _pins(worlds, shown, baseline, queries, rule, universe.rows)
    mutants: Dict[str, DefaultDict[str, List[str]]] = {}
    for mutant in universe.mutants:
        with monkeypatch.context() as patched:
            assert mutant == "per_cell"
            patched.setattr(history, "_entries", _per_cell)
            kept = {world: _digest(audit.profile(world)) for world in worlds}
        mutants[mutant] = _pins(worlds, kept, baseline, queries, rule, universe.rows)
    return {"worlds": len(worlds), "published": published, "pins": judged, "mutants": mutants}


def _audit_universe(universe: Universe, monkeypatch: pytest.MonkeyPatch) -> None:
    started = time.time()
    done = _run(universe, monkeypatch)
    assert done["worlds"] > 0
    assert done["pins"].get("named", []) == [], done["pins"]["named"][:3]
    assert done["pins"].get("bins", []) == [], done["pins"].get("bins", [])[:3]
    assert done["published"] >= universe.published, (done["published"], time.time() - started)
    for mutant in universe.mutants:
        assert done["mutants"][mutant].get("named"), f"the {mutant} mutant gives no count away"
    print(  # the table of what each universe holds, in the report of a run with ``-rP``
        f"{universe.name}: {done['worlds']} worlds, {done['published']} publish, mutants pin "
        f"{ {m: len(done['mutants'][m].get('named', [])) for m in universe.mutants} }, "
        f"{time.time() - started:.0f}s"
    )


def test_o0_a_followed_key_pins_no_named_count_or_bin_over_members_of_one_branch(monkeypatch):
    _audit_universe(UNIVERSES[0], monkeypatch)


@needs_full
@pytest.mark.timeout(1800)
def test_o1_a_followed_key_pins_no_named_count_or_bin(monkeypatch):
    _audit_universe(UNIVERSES[1], monkeypatch)


@needs_full
@pytest.mark.timeout(3600)
def test_o2_a_followed_key_pins_no_named_count_or_bin(monkeypatch):
    _audit_universe(UNIVERSES[2], monkeypatch)


@needs_full
@pytest.mark.timeout(2400)
def test_o3_a_followed_key_pins_no_named_count_over_chains_of_three(monkeypatch):
    _audit_universe(UNIVERSES[3], monkeypatch)


@needs_full
@pytest.mark.timeout(3600)
def test_o4_a_followed_key_pins_no_named_count_on_the_production_bins(monkeypatch):
    _audit_universe(UNIVERSES[4], monkeypatch)


@needs_full
@pytest.mark.timeout(2400)
def test_o5_a_followed_key_pins_no_named_count_under_a_larger_rule(monkeypatch):
    _audit_universe(UNIVERSES[5], monkeypatch)


@needs_full
@pytest.mark.timeout(2400)
def test_o7_a_followed_key_publishes_and_pins_no_named_count_where_zeros_are_not_disclosive(
    monkeypatch,
):
    _audit_universe(UNIVERSES[6], monkeypatch)


def test_o8_a_followed_key_pins_no_named_count_in_tables_of_four_rows_and_the_mutant_does(
    monkeypatch,
):
    _audit_universe(UNIVERSES[7], monkeypatch)


def test_o6_a_table_of_two_keys_is_not_followed_and_its_relationship_is_formed_as_before(
    monkeypatch,
):
    keys = ["A", "B"]
    rule = disclosure.DisclosureRule("o6", 2, False, 1, 2, "0.90", "0.10")
    monkeypatch.setattr(runner, "CHILDREN_EDGES", (0, 1, 2, disclosure.MAX_COUNT))
    audit = _Audit(monkeypatch, keys, rule)
    published = 0
    for world in _worlds(keys, 2, 6):
        formed = audit.profile(world)
        history = formed.document["tables"]["card"].get("history", {})
        assert "branch_id" not in history.get("columns", {})
        (relationship,) = formed.document["relationships"]
        published += any(
            group["status"] == "F"
            for each in relationship["statistics"]
            for group in each["groups"]
        )
        with monkeypatch.context() as patched:
            patched.setattr(references.References, "follow", lambda self, child, column: None)
            assert audit.profile(world).canonical == formed.canonical
    assert published > 0  # the relationship is published where its counts allow it
