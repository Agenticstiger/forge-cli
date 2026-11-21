# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Contribution bounding by privacy unit.

A release is calibrated to how much one *privacy unit* can change it. A unit is
one entity: one row of the unit's expose. The released table is either that
expose itself (:data:`ROWS`: each row is a unit, nothing is dropped) or a child
of it through one relationship (:data:`RELATED`: each row belongs to the unit its
``fromColumns`` name, and one unit can own many rows). A single table whose rows
carry an entity key is the third case (:data:`KEYED`): the release table is the
unit expose and a row belongs to the unit its key columns name.

Outside the own-rows mode a release bounds what a unit contributes in two stages. The
pattern is borrowed from PipelineDP's per-privacy-id bounding
(``max_partitions_contributed`` and ``max_contributions_per_partition``) and
from Tumult Analytics' ``MaxRowsPerID`` and ``MaxRowsPerGroupPerID``; neither is
a dependency.

1. Across the relationship, :func:`bound_rows` keeps at most ``c`` rows of each
   unit: the ``c`` with the smallest keyed priority. A unit's kept rows depend
   only on its own rows and the seed, never on another unit or on row order, so
   removing a unit changes no other unit's rows.
2. Per cell, :func:`capped_counts` lets a unit count at most ``k`` rows in any
   one cell of a count table.

One unit then moves a count table by at most ``q * k * k + r * r`` in squared L2
norm, with ``q, r = divmod(c, k)``. The squared sensitivity is the larger of
that and 2: one added or removed row of the released table can swap which of its
unit's rows is kept, which moves one cell up and another down, and that change
is not covered by the unit's own bounded contribution when ``c`` is 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.profiler.dp.randomness import priority

__all__ = [
    "KEYED",
    "MOST_ROWS_PER_UNIT",
    "RELATED",
    "ROWS",
    "UnitPlan",
    "UnitRefused",
    "bound_rows",
    "capped_counts",
    "encode_values",
    "plan_unit",
    "repeated_keys",
    "selection_distance",
    "sensitivity_squared",
]

ROWS = "rows"
"""The release table is the unit's own expose: each row is a unit."""

RELATED = "related"
"""The release table is a child of the unit's expose through one relationship."""

KEYED = "keyed"
"""The release table is the unit's own expose, and a row belongs to the unit its key columns name."""

MOST_ROWS_PER_UNIT = 1000
"""The largest number of rows a unit may contribute."""

_LABEL = "bound"


class UnitRefused(ValueError):
    """A privacy unit that cannot be formed. ``event`` is a stable code, ``context`` its details."""

    def __init__(self, event: str, message: str, context: Optional[Mapping[str, str]] = None):
        super().__init__(message)
        self.event = event
        self.context: Dict[str, str] = dict(context or {})


@dataclass(frozen=True)
class UnitPlan:
    """Which rows are units, and how much a unit may contribute."""

    unit_expose: str
    release_table: str
    mode: str
    #: ROWS: the unit expose's primary key columns, or () when none is declared.
    #: RELATED: the release table's fromColumns of the relationship to the unit.
    #: KEYED: the unit key columns, in the order given.
    key_columns: Tuple[str, ...]
    #: RELATED: that relationship's name.
    relationship: Optional[str]
    #: The most rows one unit contributes (``c``); 1 in ROWS mode.
    max_rows_per_unit: int
    #: The most rows one unit counts in one cell (``k``); 1 in ROWS mode.
    max_rows_per_cell: int


def _expose_map(contract: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    value = contract.get("exposes") if isinstance(contract, Mapping) else None
    found: Dict[str, Mapping[str, Any]] = {}
    for item in value if isinstance(value, list) else ():
        if isinstance(item, Mapping) and isinstance(item.get("exposeId"), str):
            found.setdefault(item["exposeId"], item)
    return found


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _semantics_list(expose: Mapping[str, Any], name: str) -> List[Any]:
    semantics = expose.get("semantics")
    value = semantics.get(name) if isinstance(semantics, Mapping) else None
    return value if isinstance(value, list) else []


def _column_list(value: object) -> Optional[Tuple[str, ...]]:
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(each, str) and each for each in value):
        return None
    return tuple(value)


def _schema_columns(expose: Mapping[str, Any]) -> Optional[Tuple[str, ...]]:
    block = expose.get("contract")
    schema = block.get("schema") if isinstance(block, Mapping) else None
    if not isinstance(schema, list):
        return None
    return tuple(
        entry["name"]
        for entry in schema
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    )


def _primary_key(unit_expose: str, expose: Mapping[str, Any]) -> Tuple[str, ...]:
    """The columns of the ``primary`` entities of *expose*: each names its column in ``expr``
    (the entity's name when ``expr`` is absent). A primary entity that names no column of
    the expose is refused."""
    columns = _schema_columns(expose)
    found: List[str] = []
    for index, entity in enumerate(_semantics_list(expose, "entities")):
        if not isinstance(entity, Mapping) or entity.get("type") != "primary":
            continue
        expr = entity.get("expr", entity.get("name"))
        if not isinstance(expr, str) or not expr or (columns is not None and expr not in columns):
            raise UnitRefused(
                "unit_key_unresolved",
                f"expose {unit_expose!r} entities[{index}] is a primary entity whose expr does "
                "not name a column of the expose; set expr to the key column",
                {"expose": unit_expose},
            )
        if expr not in found:
            found.append(expr)
    return tuple(found)


def _unit_key(
    unit_expose: str, expose: Mapping[str, Any], unit_key: Sequence[str]
) -> Tuple[str, ...]:
    """A unit key: a non-empty sequence of distinct columns of the expose's schema."""
    if isinstance(unit_key, (str, bytes)):
        raise UnitRefused(
            "unit_key_malformed",
            "a unit key is a list of column names, not one string",
            {"expose": unit_expose},
        )
    names = tuple(unit_key)
    if (
        not names
        or len(set(names)) != len(names)
        or not all(isinstance(each, str) and each for each in names)
    ):
        raise UnitRefused(
            "unit_key_malformed",
            f"the unit key of {unit_expose!r} must be a non-empty list of distinct column names",
            {"expose": unit_expose},
        )
    columns = _schema_columns(expose) or ()
    missing = [each for each in names if each not in columns]
    if missing:
        raise UnitRefused(
            "unit_key_column_unknown",
            f"the unit key of {unit_expose!r} names {missing}, not columns of its contract.schema "
            f"(columns: {sorted(columns)}); name columns the expose declares",
            {"expose": unit_expose},
        )
    return names


def _check_count(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise UnitRefused(
            "unit_bound_out_of_range",
            f"{name} must be a whole number from {low} to {high}",
            {"option": name},
        )
    return value


def plan_unit(
    contract: Mapping[str, Any],
    unit_expose: str,
    release_table: Optional[str] = None,
    *,
    unit_key: Optional[Sequence[str]] = None,
    max_rows_per_unit: Optional[int] = None,
    max_rows_per_cell: Optional[int] = None,
) -> UnitPlan:
    """Read the privacy unit and the release table from the contract.

    With *unit_key* (distinct column names of the unit expose) the release table is the
    unit expose itself and a row belongs to the unit its key values name (:data:`KEYED`).
    """
    exposes = _expose_map(contract)
    table = unit_expose if release_table is None else release_table
    for role, each in (("privacy unit", unit_expose), ("release table", table)):
        if each not in exposes:
            raise UnitRefused(
                "unit_expose_unknown",
                f"the {role} {each!r} is not an expose of the contract "
                f"(exposes: {sorted(exposes)}); name one of them",
                {"expose": str(each)},
            )
    if unit_key is not None:
        if table != unit_expose:
            raise UnitRefused(
                "unit_key_with_other_table",
                f"a unit key names columns of the release table {unit_expose!r} itself, but the "
                f"release table is {table!r}; declare a relationship from {table!r} to "
                f"{unit_expose!r} instead, or release {unit_expose!r}",
                {"unit": unit_expose, "table": table},
            )
        key = _unit_key(unit_expose, exposes[unit_expose], unit_key)
        c = 1
        if max_rows_per_unit is not None:
            c = _check_count("max rows per unit", max_rows_per_unit, 1, MOST_ROWS_PER_UNIT)
        k = c
        if max_rows_per_cell is not None:
            k = _check_count("max rows per cell", max_rows_per_cell, 1, c)
        return UnitPlan(
            unit_expose=unit_expose,
            release_table=table,
            mode=KEYED,
            key_columns=key,
            relationship=None,
            max_rows_per_unit=c,
            max_rows_per_cell=k,
        )
    if table == unit_expose:
        if max_rows_per_unit is not None or max_rows_per_cell is not None:
            raise UnitRefused(
                "unit_bound_in_rows_mode",
                f"the release table is the privacy unit {unit_expose!r}, so each row is a unit "
                "and nothing is dropped; leave max rows per unit and per cell out, or release "
                "a child table of the unit",
                {"expose": unit_expose},
            )
        return UnitPlan(
            unit_expose=unit_expose,
            release_table=table,
            mode=ROWS,
            key_columns=_primary_key(unit_expose, exposes[unit_expose]),
            relationship=None,
            max_rows_per_unit=1,
            max_rows_per_cell=1,
        )
    declared = [
        entry
        for entry in _semantics_list(exposes[table], "relationships")
        if isinstance(entry, Mapping) and entry.get("to") == unit_expose
    ]
    where = f"release table {table!r} and privacy unit {unit_expose!r}"
    if not declared:
        raise UnitRefused(
            "unit_relationship_missing",
            f"{where}: {table!r} declares no semantics.relationships entry whose `to` is "
            f"{unit_expose!r}. Only one hop is supported, a direct relationship from the "
            "release table to the unit; declare it in the contract, or name another unit",
            {"unit": unit_expose, "table": table},
        )
    if len(declared) > 1:
        names = sorted(str(entry.get("name")) for entry in declared)
        raise UnitRefused(
            "unit_relationship_ambiguous",
            f"{where}: {table!r} declares {len(declared)} relationships to {unit_expose!r} "
            f"({', '.join(names)}), so the unit of a row is not one thing; keep exactly one",
            {"unit": unit_expose, "table": table},
        )
    entry = declared[0]
    name = entry.get("name")
    label = name if isinstance(name, str) and name else "?"
    from_columns = _column_list(entry.get("fromColumns"))
    to_columns = _column_list(entry.get("toColumns"))
    if from_columns is None or to_columns is None or len(from_columns) != len(to_columns):
        raise UnitRefused(
            "unit_relationship_malformed",
            f"{where}: relationship {label!r} needs fromColumns and toColumns, two non-empty "
            "lists of column names of one length; fix it in the contract",
            {"unit": unit_expose, "table": table, "relationship": label},
        )
    c = 1
    if max_rows_per_unit is not None:
        c = _check_count("max rows per unit", max_rows_per_unit, 1, MOST_ROWS_PER_UNIT)
    k = c
    if max_rows_per_cell is not None:
        k = _check_count("max rows per cell", max_rows_per_cell, 1, c)
    return UnitPlan(
        unit_expose=unit_expose,
        release_table=table,
        mode=RELATED,
        key_columns=from_columns,
        relationship=label,
        max_rows_per_unit=c,
        max_rows_per_cell=k,
    )


def sensitivity_squared(plan: UnitPlan) -> int:
    """The squared L2 sensitivity of one count table to one unit and to one row."""
    if plan.mode == ROWS:
        return 1
    q, r = divmod(plan.max_rows_per_unit, plan.max_rows_per_cell)
    return max(q * plan.max_rows_per_cell * plan.max_rows_per_cell + r * r, 2)


def selection_distance(plan: UnitPlan) -> int:
    """The symmetric distance between neighbouring tables, for the selection step."""
    return 1 if plan.mode == ROWS else max(plan.max_rows_per_unit, 2)


def encode_values(values: Sequence[object]) -> bytes:
    """An injective byte encoding of a sequence of read values.

    Each value is a one-byte type tag, the length of its UTF-8 text as 8 bytes
    big-endian, then the text (a ``Fraction`` as ``numerator/denominator``).
    """
    out = bytearray()
    for value in values:
        if value is None:
            tag, text = b"N", ""
        elif isinstance(value, bool):
            raise TypeError("a bool is not a read value")
        elif isinstance(value, str):
            tag, text = b"S", value
        elif isinstance(value, int):
            tag, text = b"I", str(value)
        elif isinstance(value, Fraction):
            tag, text = b"F", f"{value.numerator}/{value.denominator}"
        else:
            raise TypeError(f"cannot encode a value of type {type(value).__name__}")
        raw = text.encode("utf-8", "surrogatepass")
        out += tag + len(raw).to_bytes(8, "big") + raw
    return bytes(out)


def bound_rows(
    units: Sequence[Optional[bytes]],
    contents: Sequence[bytes],
    seed: bytes,
    max_rows: int,
) -> List[int]:
    """The indices of the rows kept, ascending: at most *max_rows* per unit.

    Row ``i`` belongs to unit ``units[i]``; ``None`` is a unit of its own and always
    keeps its row.
    """
    if len(units) != len(contents):
        raise ValueError("units and contents must have one entry per row")
    if not _is_int(max_rows) or max_rows < 1:
        raise ValueError("max_rows must be a whole number of at least 1")
    kept: List[int] = []
    groups: Dict[bytes, List[Tuple[bytes, bytes, int]]] = {}
    for i, unit in enumerate(units):
        if unit is None:
            kept.append(i)
            continue
        key = len(unit).to_bytes(8, "big") + unit + contents[i]
        groups.setdefault(unit, []).append((priority(seed, _LABEL, key), contents[i], i))
    for rows in groups.values():
        rows.sort()
        kept.extend(i for _, _, i in rows[:max_rows])
    kept.sort()
    return kept


def capped_counts(
    units: Sequence[Optional[bytes]], cells: Sequence[int], size: int, cap: int
) -> List[int]:
    """One count per cell of a table of *size* cells; a unit counts at most *cap* rows per cell."""
    if len(units) != len(cells):
        raise ValueError("units and cells must have one entry per row")
    if not _is_int(size) or size < 0:
        raise ValueError("size must not be negative")
    if not _is_int(cap) or cap < 1:
        raise ValueError("cap must be a whole number of at least 1")
    counts = [0] * size
    seen: Dict[Tuple[bytes, int], int] = {}
    for unit, cell in zip(units, cells, strict=True):
        if not _is_int(cell) or not 0 <= cell < size:
            raise ValueError("a cell index is outside the table")
        if unit is not None:
            held = seen.get((unit, cell), 0)
            if held >= cap:
                continue
            seen[(unit, cell)] = held + 1
        counts[cell] += 1
    return counts


def repeated_keys(keys: Sequence[Optional[bytes]]) -> Tuple[int, int]:
    """``(key values that occur more than once, rows with a null key part)``.

    A ``None`` entry is a row with a null part; it is not counted as a repeat.
    """
    seen: Dict[bytes, int] = {}
    nulls = 0
    for key in keys:
        if key is None:
            nulls += 1
        else:
            seen[key] = seen.get(key, 0) + 1
    return sum(1 for n in seen.values() if n > 1), nulls
