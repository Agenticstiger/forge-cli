# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Optional column-level profiler for Parquet / Avro sample files.

The profiler is deliberately a *soft* extension of :mod:`parser`: the LLM
pipeline can consume richer column statistics (observed nullability,
distinct-count estimate, sample values) when sample data is supplied, but
the whole module short-circuits cleanly if ``pyarrow`` / ``fastavro`` are
not installed or no sample files are available. No hard dependency is
added by importing this module.

Public entry points
-------------------
* :func:`sample_columnar_file` — profile a single Parquet/Avro file.
* :func:`sample_directory` — walk a directory tree, profile every
  Parquet/Avro file found.
* :func:`merge_profile_into_tables` — splice :class:`TableProfile`
  statistics into a list of :class:`TableDefinition` produced by
  :class:`DDLParser`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fluid_build.forge_datamodel.from_ddl.parser import (
    ColumnDefinition,
    TableDefinition,
)

_PARQUET_SUFFIXES = {".parquet", ".pq"}
_AVRO_SUFFIXES = {".avro", ".avsc"}


@dataclass
class ColumnStats:
    """Per-column observations gathered from a sample file."""

    name: str
    observed_type: Optional[str] = None
    null_rate: Optional[float] = None
    distinct_count: Optional[int] = None
    sample_values: List[Any] = field(default_factory=list)


@dataclass
class TableProfile:
    """Row-level profile for one sample file."""

    source_path: Path
    row_count: int = 0
    column_stats: List[ColumnStats] = field(default_factory=list)

    def by_name(self) -> Dict[str, ColumnStats]:
        return {col.name: col for col in self.column_stats}


def sample_columnar_file(
    path: Path | str, *, max_rows: int = 1000, max_distinct: int = 128
) -> Optional[TableProfile]:
    """Profile one Parquet or Avro file; return ``None`` if unreadable.

    Imports ``pyarrow`` for Parquet and ``fastavro`` for Avro lazily so
    missing optional deps never break an import of this module.
    """
    target = Path(path)
    if not target.is_file():
        return None
    suffix = target.suffix.lower()
    if suffix in _PARQUET_SUFFIXES:
        return _profile_parquet(target, max_rows=max_rows, max_distinct=max_distinct)
    if suffix in _AVRO_SUFFIXES:
        return _profile_avro(target, max_rows=max_rows, max_distinct=max_distinct)
    return None


def sample_directory(
    directory: Path | str,
    *,
    max_rows: int = 1000,
    max_distinct: int = 128,
) -> List[TableProfile]:
    """Walk ``directory`` and profile every recognised columnar file."""
    root = Path(directory)
    if not root.is_dir():
        return []
    profiles: List[TableProfile] = []
    for child in sorted(root.rglob("*")):
        profile = sample_columnar_file(child, max_rows=max_rows, max_distinct=max_distinct)
        if profile is not None:
            profiles.append(profile)
    return profiles


def merge_profile_into_tables(
    tables: Sequence[TableDefinition],
    profiles: Iterable[TableProfile],
) -> List[TableDefinition]:
    """Attach profile-derived hints to parsed table columns.

    Matching is by table name: the profile's source filename stem is
    compared (case-insensitive) against each :class:`TableDefinition`
    name. When a match is found, columns present in both are enriched
    with the observed ``null_rate`` and ``distinct_count`` under
    :attr:`ColumnDefinition.qualifiers` so downstream stages can
    reason about it without changing the core dataclass shape.
    """
    profile_by_stem: Dict[str, TableProfile] = {
        profile.source_path.stem.lower(): profile for profile in profiles
    }
    enriched: List[TableDefinition] = []
    for table in tables:
        profile = profile_by_stem.get(table.name.lower())
        if profile is None:
            enriched.append(table)
            continue
        stats_by_name = profile.by_name()
        new_columns: List[ColumnDefinition] = []
        for column in table.columns:
            stats = stats_by_name.get(column.name)
            if stats is None:
                new_columns.append(column)
                continue
            qualifiers = dict(column.qualifiers)
            qualifiers.setdefault("profile", {}).update(
                {
                    "observed_type": stats.observed_type,
                    "null_rate": stats.null_rate,
                    "distinct_count": stats.distinct_count,
                    "sample_values": stats.sample_values[:8],
                }
            )
            new_columns.append(
                ColumnDefinition(
                    name=column.name,
                    logical_type=column.logical_type,
                    qualifiers=qualifiers,
                    nullable=column.nullable,
                    primary_key=column.primary_key,
                    comment=column.comment,
                )
            )
        enriched.append(
            TableDefinition(
                name=table.name,
                columns=new_columns,
                primary_keys=list(table.primary_keys),
                comment=table.comment,
            )
        )
    return enriched


# ---------------------------------------------------------------------------
# Backend adapters — fully soft-imported so a missing optional dep is benign.
# ---------------------------------------------------------------------------


def _profile_parquet(path: Path, *, max_rows: int, max_distinct: int) -> Optional[TableProfile]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        return None
    try:
        table = pq.read_table(path)
    except Exception:
        return None
    if table.num_rows > max_rows:
        table = table.slice(0, max_rows)
    stats: List[ColumnStats] = []
    for column_name in table.schema.names:
        column = table.column(column_name)
        values = column.to_pylist()
        nulls = sum(1 for v in values if v is None)
        distinct = _distinct_count(values, max_distinct=max_distinct)
        stats.append(
            ColumnStats(
                name=column_name,
                observed_type=str(column.type),
                null_rate=(nulls / len(values)) if values else None,
                distinct_count=distinct,
                sample_values=[v for v in values[:8] if v is not None],
            )
        )
    return TableProfile(source_path=path, row_count=table.num_rows, column_stats=stats)


def _profile_avro(path: Path, *, max_rows: int, max_distinct: int) -> Optional[TableProfile]:
    try:
        from fastavro import reader  # type: ignore
    except Exception:
        return None
    try:
        with path.open("rb") as handle:
            records: List[Dict[str, Any]] = []
            for row_index, record in enumerate(reader(handle)):
                if row_index >= max_rows:
                    break
                records.append(record)
    except Exception:
        return None
    if not records:
        return TableProfile(source_path=path, row_count=0, column_stats=[])
    columns: Dict[str, List[Any]] = {}
    for record in records:
        for name, value in record.items():
            columns.setdefault(name, []).append(value)
    stats: List[ColumnStats] = []
    for name, values in columns.items():
        nulls = sum(1 for v in values if v is None)
        stats.append(
            ColumnStats(
                name=name,
                observed_type=_infer_python_type(values),
                null_rate=(nulls / len(values)) if values else None,
                distinct_count=_distinct_count(values, max_distinct=max_distinct),
                sample_values=[v for v in values[:8] if v is not None],
            )
        )
    return TableProfile(source_path=path, row_count=len(records), column_stats=stats)


def _distinct_count(values: Sequence[Any], *, max_distinct: int) -> Optional[int]:
    """Estimate distinct count, capped so we never materialise huge sets."""
    if not values:
        return 0
    seen: set[Any] = set()
    for value in values:
        if value is None:
            continue
        try:
            seen.add(value)
        except TypeError:
            # Unhashable (list / dict). Fall back to repr so we still return
            # a meaningful upper bound without raising.
            seen.add(repr(value))
        if len(seen) >= max_distinct:
            return max_distinct
    return len(seen)


def _infer_python_type(values: Sequence[Any]) -> Optional[str]:
    non_null = [type(v).__name__ for v in values if v is not None]
    if not non_null:
        return None
    # Most-common observed python type for the column (stable order).
    counts: Dict[str, int] = {}
    for name in non_null:
        counts[name] = counts.get(name, 0) + 1
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]
