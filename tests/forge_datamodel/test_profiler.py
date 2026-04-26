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

"""Coverage for the optional Parquet/Avro profiler (plan-gap A5).

The profiler is a *soft* extension of :mod:`fluid_build.forge_datamodel.from_ddl.parser`:
when sample data is supplied it enriches every parsed column with
observed nullability, distinct-count, and a clipped sample-values list.
The whole module must short-circuit cleanly when the optional
``pyarrow`` / ``fastavro`` dependencies are missing — adding the
profiler must not impose a hard dependency on either backend.

These tests pin three layers:

* **Pure-Python helpers** (``_distinct_count``, ``_infer_python_type``,
  ``merge_profile_into_tables``) — exercised without any optional
  dependency.
* **Backend dispatch** (``sample_columnar_file``, ``sample_directory``)
  — must return ``None`` / ``[]`` for missing files, unrecognised
  extensions, or missing optional deps.
* **Round-trip** (Parquet only — ``pyarrow`` is widely available;
  Avro is skipped when ``fastavro`` is absent so the suite still
  runs in minimal environments).

The Parquet round-trip exercises ``ColumnStats.observed_type``,
``null_rate``, ``distinct_count``, ``sample_values`` and
:func:`merge_profile_into_tables` together — that is the path the
modeler actually uses to enrich its DDL parse.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, List

import pytest

from fluid_build.forge_datamodel.from_ddl.parser import (
    ColumnDefinition,
    TableDefinition,
)
from fluid_build.forge_datamodel.from_ddl.profiler import (
    ColumnStats,
    TableProfile,
    _distinct_count,
    _infer_python_type,
    merge_profile_into_tables,
    sample_columnar_file,
    sample_directory,
)


def _has_module(name: str) -> bool:
    """Soft-import probe used to scope live-backend tests."""
    try:
        importlib.import_module(name)
    except Exception:
        return False
    return True


_HAS_PYARROW = _has_module("pyarrow.parquet")


# ----------------------------------------------------------------------
# _distinct_count — capping and unhashable-value fallback
# ----------------------------------------------------------------------


class TestDistinctCount:
    def test_empty_list_returns_zero(self):
        assert _distinct_count([], max_distinct=128) == 0

    def test_simple_unique_values(self):
        assert _distinct_count([1, 2, 3], max_distinct=128) == 3

    def test_repeats_collapse(self):
        assert _distinct_count(["a", "a", "b"], max_distinct=128) == 2

    def test_none_values_skipped(self):
        """None must not be counted toward the distinct set — it
        represents "absent" semantically, not a value."""
        assert _distinct_count([None, "a", None, "b"], max_distinct=128) == 2

    def test_capping_returns_max_distinct(self):
        """The cap is a load-bearing memory guard: a column with 10M
        distinct values must not materialise a 10M-entry set."""
        assert _distinct_count(list(range(1000)), max_distinct=10) == 10

    def test_unhashable_values_fallback_to_repr(self):
        """Lists / dicts are unhashable; the helper must still produce
        a useful upper bound rather than crashing."""
        # Two distinct lists, repr'd into the set as their string forms.
        assert _distinct_count([[1], [1], [2]], max_distinct=128) == 2


# ----------------------------------------------------------------------
# _infer_python_type — most-common type wins, deterministically
# ----------------------------------------------------------------------


class TestInferPythonType:
    def test_returns_none_for_empty(self):
        assert _infer_python_type([]) is None

    def test_returns_none_for_all_null(self):
        assert _infer_python_type([None, None]) is None

    def test_majority_type_wins(self):
        assert _infer_python_type([1, 2, "x"]) == "int"

    def test_ties_break_lexicographically(self):
        """When two types are equally common, the helper must pick a
        deterministic winner (here: ``str`` > ``int`` lexicographically)
        so two consecutive runs against the same data agree."""
        assert _infer_python_type([1, "x"]) == "str"


# ----------------------------------------------------------------------
# sample_columnar_file — dispatch + soft-fail on missing/unknown paths
# ----------------------------------------------------------------------


class TestSampleColumnarFile:
    def test_missing_path_returns_none(self, tmp_path: Path):
        assert sample_columnar_file(tmp_path / "nope.parquet") is None

    def test_unrecognised_suffix_returns_none(self, tmp_path: Path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        assert sample_columnar_file(f) is None

    def test_directory_argument_returns_none(self, tmp_path: Path):
        """Passing a directory in must not crash — the helper is for
        files and should soft-fail to ``None``."""
        assert sample_columnar_file(tmp_path) is None


# ----------------------------------------------------------------------
# sample_directory — walk + collect
# ----------------------------------------------------------------------


class TestSampleDirectory:
    def test_missing_directory_returns_empty(self, tmp_path: Path):
        assert sample_directory(tmp_path / "nope") == []

    def test_empty_directory_returns_empty(self, tmp_path: Path):
        assert sample_directory(tmp_path) == []

    def test_directory_with_only_unsupported_files_returns_empty(self, tmp_path: Path):
        (tmp_path / "a.csv").write_text("x", encoding="utf-8")
        (tmp_path / "b.json").write_text("{}", encoding="utf-8")
        assert sample_directory(tmp_path) == []


# ----------------------------------------------------------------------
# merge_profile_into_tables — splice profile stats into parsed tables
# ----------------------------------------------------------------------


def _table(name: str, *cols: str) -> TableDefinition:
    return TableDefinition(
        name=name,
        columns=[ColumnDefinition(name=c, logical_type="string") for c in cols],
    )


def _profile(stem: str, **stats: Any) -> TableProfile:
    """Build a ``TableProfile`` whose ``source_path.stem`` matches ``stem``."""
    cols: List[ColumnStats] = []
    for name, observed in stats.items():
        cols.append(
            ColumnStats(
                name=name,
                observed_type=observed.get("observed_type"),
                null_rate=observed.get("null_rate"),
                distinct_count=observed.get("distinct_count"),
                sample_values=observed.get("sample_values", []),
            )
        )
    return TableProfile(source_path=Path(f"/tmp/{stem}.parquet"), row_count=10, column_stats=cols)


class TestMergeProfileIntoTables:
    def test_unmatched_table_returned_unchanged(self):
        tables = [_table("orders", "id", "amount")]
        profiles = [_profile("customers", id={"observed_type": "int64"})]
        merged = merge_profile_into_tables(tables, profiles)
        assert merged == tables

    def test_matched_columns_get_profile_qualifiers(self):
        tables = [_table("orders", "id", "amount")]
        profiles = [
            _profile(
                "orders",
                id={"observed_type": "int64", "null_rate": 0.0, "distinct_count": 10},
                amount={
                    "observed_type": "double",
                    "null_rate": 0.1,
                    "distinct_count": 9,
                    "sample_values": [1.0, 2.0, 3.0],
                },
            )
        ]
        merged = merge_profile_into_tables(tables, profiles)
        assert len(merged) == 1
        amount_col = next(c for c in merged[0].columns if c.name == "amount")
        profile = amount_col.qualifiers["profile"]
        assert profile["observed_type"] == "double"
        assert profile["null_rate"] == 0.1
        assert profile["distinct_count"] == 9
        assert profile["sample_values"] == [1.0, 2.0, 3.0]

    def test_columns_not_in_profile_left_alone(self):
        tables = [_table("orders", "id", "amount", "extra")]
        profiles = [_profile("orders", id={"observed_type": "int64"})]
        merged = merge_profile_into_tables(tables, profiles)
        amount_col = next(c for c in merged[0].columns if c.name == "amount")
        extra_col = next(c for c in merged[0].columns if c.name == "extra")
        # No profile section emitted for un-profiled columns.
        assert "profile" not in amount_col.qualifiers
        assert "profile" not in extra_col.qualifiers

    def test_match_is_case_insensitive(self):
        """``orders.parquet`` should match a table named ``Orders`` —
        warehouses are inconsistent about casing and this lets the
        modeler pull profile data without re-mangling the source."""
        tables = [_table("Orders", "id")]
        profiles = [_profile("orders", id={"observed_type": "int64"})]
        merged = merge_profile_into_tables(tables, profiles)
        col = merged[0].columns[0]
        assert col.qualifiers["profile"]["observed_type"] == "int64"

    def test_sample_values_clipped_to_eight(self):
        tables = [_table("orders", "id")]
        big_samples = list(range(20))
        profiles = [_profile("orders", id={"observed_type": "int64", "sample_values": big_samples})]
        merged = merge_profile_into_tables(tables, profiles)
        col = merged[0].columns[0]
        assert col.qualifiers["profile"]["sample_values"] == big_samples[:8]

    def test_table_metadata_preserved_on_merge(self):
        """Primary-key list and table-level comment must survive the
        merge — the helper only touches per-column qualifiers."""
        tables = [
            TableDefinition(
                name="orders",
                columns=[ColumnDefinition(name="id", logical_type="int")],
                primary_keys=["id"],
                comment="orders table",
            )
        ]
        profiles = [_profile("orders", id={"observed_type": "int64"})]
        merged = merge_profile_into_tables(tables, profiles)
        assert merged[0].primary_keys == ["id"]
        assert merged[0].comment == "orders table"


# ----------------------------------------------------------------------
# Parquet round-trip — exercises the actual pyarrow backend when present
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    not _HAS_PYARROW,
    reason="pyarrow not installed — live Parquet round-trip tests skipped",
)
class TestParquetRoundTrip:
    """Live Parquet path — only runs when ``pyarrow`` is installed.

    These tests are the only ones that actually exercise the dispatch
    inside :func:`_profile_parquet`. Skipping them in environments
    without ``pyarrow`` keeps the rest of the suite fast and dep-free.
    """

    def test_profiles_a_real_parquet_file(self, tmp_path: Path):
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table(
            {
                "id": [1, 2, 3, 4],
                "name": ["alice", "bob", None, "alice"],
            }
        )
        path = tmp_path / "people.parquet"
        pq.write_table(table, path)
        profile = sample_columnar_file(path)
        assert profile is not None
        assert profile.row_count == 4
        stats = {c.name: c for c in profile.column_stats}
        assert stats["id"].null_rate == 0.0
        assert stats["id"].distinct_count == 4
        assert stats["name"].null_rate == 0.25
        assert stats["name"].distinct_count == 2

    def test_max_rows_truncates_before_profiling(self, tmp_path: Path):
        """``max_rows`` must clip the input before counting nulls /
        distincts, so a 1B-row file profiles in milliseconds."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table({"id": list(range(100))})
        path = tmp_path / "big.parquet"
        pq.write_table(table, path)
        profile = sample_columnar_file(path, max_rows=10)
        assert profile is not None
        assert profile.row_count == 10
        assert profile.column_stats[0].distinct_count == 10

    def test_directory_walk_picks_up_parquet_files(self, tmp_path: Path):
        import pyarrow as pa
        import pyarrow.parquet as pq

        for name in ("a", "b"):
            t = pa.table({"id": [1, 2, 3]})
            pq.write_table(t, tmp_path / f"{name}.parquet")
        # Mix in an unrecognised file — must be ignored.
        (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
        profiles = sample_directory(tmp_path)
        assert {p.source_path.stem for p in profiles} == {"a", "b"}


# ----------------------------------------------------------------------
# Soft-fail when pyarrow is missing — simulated via import shim
# ----------------------------------------------------------------------


class TestSoftFailOnMissingBackend:
    def test_parquet_returns_none_when_pyarrow_unavailable(self, tmp_path: Path, monkeypatch):
        """Replace ``pyarrow.parquet`` in ``sys.modules`` with one that
        raises on import-time so the lazy ``import pyarrow.parquet``
        inside ``_profile_parquet`` falls into the broad ``except``
        and returns ``None``. This pins the soft-fail contract: missing
        optional dep is *never* a crash, only a soft skip."""
        import sys

        # Build a real .parquet file so the path-recognition step succeeds.
        path = tmp_path / "data.parquet"
        path.write_bytes(b"PAR1\x00")  # invalid parquet body, but recognisable suffix
        # Force the lazy import inside _profile_parquet to fail.
        monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
        # Reload to ensure no cached working backend leaks in.
        import fluid_build.forge_datamodel.from_ddl.profiler as profiler_module

        importlib.reload(profiler_module)
        try:
            assert profiler_module.sample_columnar_file(path) is None
        finally:
            # Cleanup — restore the real module so other tests aren't poisoned.
            monkeypatch.delitem(sys.modules, "pyarrow.parquet", raising=False)
            importlib.reload(profiler_module)
