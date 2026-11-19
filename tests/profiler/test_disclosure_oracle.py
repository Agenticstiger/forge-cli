# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An exhaustive audit of the count tables a profile publishes: no small count is pinned.

For tiny seeded instances (one or two columns, at most three bins, at most seven rows)
this enumerates every world, every multiset of records, runs the real profiler on each
(the registry, the output checks and the document, through ``profile_exposes``), and
groups the worlds by the document they publish. Worlds that publish the same document
are the worlds a reader cannot tell apart, the publish decision included: a withheld
cell is the same ``C`` in each. A count is *pinned* when every world of its group gives
it one value. The audit fails on a count the rule protects (1 to ``minimumCell - 1``
records, or none where zeros are disclosive) that the new statistics pin and the
document without them does not: a count of one cell, or of any run of adjacent cells,
of each table, so the three cells of a ``quantile/2`` are among them.

A pin the old document already gave is not this change's, and is not counted against
it: a published ``min`` shows that no record lies below it (PROFILE-SPEC §3, outside
ADR 0012's rule). The rule is scaled down (a minimum cell of 2 to 4, rounding to 1 or 2)
so that seven records can publish anything, with the same structure as ACRO's defaults.
A group of worlds is judged only when the universe holds all of them: every world of it
has at most ``MAX_ROWS - rounding_base`` records, so no larger world is missing from it.

What it found, and what the profiler does about it:

- a column's ``distinctCount`` says how many of a histogram's withheld cells hold no
  record, so a column a level histogram counts withholds it;
- a number column's exact ``sum`` (and ``mean/1``, its sum on the grid), its
  ``quantiles`` and its ``distinctCount`` give a withheld bin back from the published
  ones, so a number column the contract's block bins withholds them and forms no
  ``mean/1``;
- a histogram and quantiles on the same bins, checked together, let a reader who knows
  the check's choices tell worlds apart, so a column gets one or the other.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler import disclosure, runner, sources
from fluid_build.profiler._stats import load_backend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.fidelity")

#: The most records a world holds.
MAX_ROWS = 7

RULES = [
    disclosure.DisclosureRule(
        name=f"oracle-m{cell}-r{base}",
        minimum_cell=cell,
        zeros_disclosive=True,
        rounding_base=base,
        dominance_n=2,
        dominance_k="0.90",
        p_ratio="0.10",
    )
    for cell, base in ((2, 1), (3, 1), (3, 2), (4, 2))
]

Row = Tuple[Optional[str], ...]
#: A protected count: its name, and the function that counts it in a world.
Quantity = Tuple[str, Callable[[Sequence[Row]], int]]


def _contract(tables: Mapping[str, Sequence[Tuple[str, str, Mapping[str, Any]]]], dims=None):
    exposes = []
    for name, columns in tables.items():
        expose: Dict[str, Any] = {
            "exposeId": name,
            "contract": {"schema": [{"name": c, "type": t, **extra} for c, t, extra in columns]},
        }
        if dims and name in dims:
            expose["semantics"] = {
                "dimensions": [{"name": d, "type": "categorical"} for d in dims[name]]
            }
        exposes.append(expose)
    return {"fluidVersion": "0.7.7", "kind": "DataProduct", "id": "oracle", "exposes": exposes}


def _worlds(values: Sequence[Row], max_rows: int) -> List[Tuple[Row, ...]]:
    """Every multiset of at most *max_rows* records drawn from *values*."""
    worlds: List[Tuple[Row, ...]] = []
    for size in range(max_rows + 1):
        worlds.extend(itertools.combinations_with_replacement(values, size))
    return worlds


def _spans(name: str, cells: Sequence[Callable[[Row], bool]]) -> List[Quantity]:
    """The count of each run of adjacent cells, for cells given as membership tests."""
    found: List[Quantity] = []
    for low in range(len(cells)):
        for high in range(low + 1, len(cells) + 1):
            members = cells[low:high]

            def count(world: Sequence[Row], members=members) -> int:
                return sum(1 for row in world if any(test(row) for test in members))

            found.append((f"{name}[{low}:{high}]", count))
    return found


def _without_new_statistics(document: Mapping[str, Any]) -> str:
    """The document as it was before the contract's default statistics: mean/1 alone."""
    import json

    stripped = json.loads(json.dumps(document))
    stripped.pop("_schemaURL", None)
    for table in stripped["tables"].values():
        for column in table["columns"].values():
            kept = [s for s in column.get("statistics", []) if s["definition"] == "mean/1"]
            if kept:
                column["statistics"] = kept
            else:
                column.pop("statistics", None)
    return json.dumps(stripped, sort_keys=True)


def _audit(
    monkeypatch: pytest.MonkeyPatch,
    contract: Mapping[str, Any],
    columns: Sequence[str],
    values: Sequence[Row],
    quantities: Sequence[Quantity],
    rule: disclosure.DisclosureRule,
    max_rows: int = MAX_ROWS,
) -> Tuple[int, List[str]]:
    """Profile every world; return how many it ran and each count the change pins."""
    backend = load_backend()
    (expose,) = [e["exposeId"] for e in contract["exposes"]]
    current: Dict[str, Tuple[Row, ...]] = {}

    def read_columns(source, names, *, table=None, cwd=None):
        world = current["world"]
        return sources.SourceColumns(
            description="world",
            row_count=len(world),
            values={name: [row[columns.index(name)] for row in world] for name in names},
        )

    monkeypatch.setattr(sources, "read_columns", read_columns)
    new_groups: Dict[bytes, List[Tuple[Row, ...]]] = defaultdict(list)
    old_view: Dict[Tuple[Row, ...], str] = {}
    old_groups: Dict[str, List[Tuple[Row, ...]]] = defaultdict(list)
    worlds = _worlds(values, max_rows)
    for world in worlds:
        current["world"] = world
        result = runner.profile_exposes(
            contract, [runner.ExposeSource(expose, "world")], backend=backend, rule=rule
        )
        new_groups[result.canonical].append(world)
        old_view[world] = _without_new_statistics(result.document)
        old_groups[old_view[world]].append(world)
    complete = max_rows - rule.rounding_base
    pins: List[str] = []
    for group in new_groups.values():
        if any(len(world) > complete for world in group):
            continue  # a larger world, outside the universe, may publish this document too
        for name, count in quantities:
            seen = {count(world) for world in group}
            if len(seen) != 1:
                continue
            (value,) = seen
            if not (value < rule.minimum_cell and (value > 0 or rule.zeros_disclosive)):
                continue  # not a count the rule protects
            before = {count(world) for world in old_groups[old_view[group[0]]]}
            if len(before) > 1:
                pins.append(f"{rule.name}: {name} = {value} in worlds like {group[0]}")
    return len(worlds), pins


LEVELS = ("a", "b", "c")


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_a_level_histogram_pins_no_small_count(monkeypatch, rule):
    contract = _contract({"t": [("s", "enum('a', 'b', 'c')", {})]})
    values: List[Row] = [(level,) for level in LEVELS] + [(None,)]
    cells = [lambda row, level=level: row[0] == level for level in LEVELS]
    ran, pins = _audit(monkeypatch, contract, ["s"], values, _spans("s", cells), rule)
    assert ran == 330
    assert pins == []


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_a_histogram_and_its_quantiles_over_a_declared_range_pin_no_small_count(monkeypatch, rule):
    # between 0 and 2 on an integer column: bins [0, 1), [1, 2) and [2, 2.5], one per value.
    declared = {"validationRules": [{"type": "range", "constraint": "between 0 and 2"}]}
    contract = _contract({"t": [("v", "int", declared)]})
    values: List[Row] = [("0",), ("1",), ("2",), (None,)]
    cells = [lambda row, value=value: row[0] == value for value in ("0", "1", "2")]
    ran, pins = _audit(monkeypatch, contract, ["v"], values, _spans("v", cells), rule)
    assert ran == 330
    assert pins == []


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_a_grouped_level_histogram_pins_no_small_count(monkeypatch, rule):
    contract = _contract(
        {"t": [("s", "enum('a', 'b')", {}), ("g", "enum('x', 'y')", {})]}, dims={"t": ["g"]}
    )
    values: List[Row] = [(s, g) for s in ("a", "b") for g in ("x", "y")]
    quantities: List[Quantity] = []
    for g in ("x", "y"):
        cells = [lambda row, s=s, g=g: row == (s, g) for s in ("a", "b")]
        quantities.extend(_spans(f"s|g={g}", cells))
    ran, pins = _audit(monkeypatch, contract, ["s", "g"], values, quantities, rule, max_rows=6)
    assert ran == 210
    assert pins == []


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_a_grouped_level_histogram_with_nulls_pins_no_small_count(monkeypatch, rule):
    # A record can hold a level and a null key, or neither: those cells are protected too.
    # The grouped statistic's sampleSize once banded the records its groups hold, and
    # beside the columns' own counts that pinned them, under rules that round or not.
    contract = _contract(
        {"t": [("s", "enum('a', 'b')", {}), ("g", "enum('x', 'y')", {})]}, dims={"t": ["g"]}
    )
    levels, keys = ("a", None), ("x", None)
    values: List[Row] = [(s, g) for s in levels for g in keys]
    quantities: List[Quantity] = []
    for g in keys:
        cells = [lambda row, s=s, g=g: row == (s, g) for s in levels]
        quantities.extend(_spans(f"s|g={g}", cells))
    for s in levels:
        cells = [lambda row, s=s, g=g: row == (s, g) for g in keys]
        quantities.extend(_spans(f"g|s={s}", cells))
    ran, pins = _audit(monkeypatch, contract, ["s", "g"], values, quantities, rule)
    assert ran == 330
    assert pins == []


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_quantiles_on_a_types_ladder_pin_no_small_count(monkeypatch, rule):
    # No declared range: quantile/2 at the quartiles and the median on the int64 ladder,
    # whose bins near these values are [-1, 0), [0, 1), [1, 2), [2, 5) and [5, 10).
    contract = _contract({"t": [("v", "int", {})]})
    values: List[Row] = [("0",), ("1",), ("2",), (None,)]
    cells = [
        lambda row: row[0] is not None and int(row[0]) < 0,
        lambda row: row[0] == "0",
        lambda row: row[0] == "1",
        lambda row: row[0] == "2",
        lambda row: row[0] is not None and int(row[0]) >= 5,
    ]
    ran, pins = _audit(monkeypatch, contract, ["v"], values, _spans("v", cells), rule)
    assert ran == 330
    assert pins == []
