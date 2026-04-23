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

"""Slice UX-G: regression tests for AI-mode latency optimisations.

Three targeted caches / caps land in slice UX-G:

1. ``forge_copilot_discovery`` grows a hard depth cap
   (``MAX_DISCOVERY_DEPTH``) and yields from the BFS queue only while
   walking within it.  ``MAX_SAMPLE_FILES`` drops from 12 to 6.

2. ``forge_copilot_runtime.build_capability_matrix`` is memoized for
   the lifetime of the process behind a lock, with
   ``clear_capability_matrix_cache`` as the invalidation hook.

3. ``forge_copilot_schema_inference.summarize_sample_file`` is
   memoized keyed on ``(resolved path, mtime_ns, size)``, with
   ``clear_sample_file_cache`` as the invalidation hook.  Caller
   mutations to the returned dict must not poison the cache.
   ``MAX_SAMPLE_ROWS`` drops from 20 to 5.

These tests assert the behavioural contracts that hold the latency
wins together — not the absolute timings (which drift with CI
runner speed) but the correctness of the caching and the bounds.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fluid_build.cli.forge_copilot_discovery import (
    MAX_DISCOVERY_DEPTH,
    MAX_DISCOVERY_FILES,
    MAX_SAMPLE_FILES,
    _iter_candidate_files,
)
from fluid_build.cli.forge_copilot_runtime import (
    build_capability_matrix,
    clear_capability_matrix_cache,
)
from fluid_build.cli.forge_copilot_schema_inference import (
    MAX_SAMPLE_ROWS,
    clear_sample_file_cache,
    summarize_sample_file,
)

# ---------------------------------------------------------------------------
# Slice UX-G caps
# ---------------------------------------------------------------------------


class TestSliceUXGCaps:
    def test_discovery_depth_cap_is_bounded(self):
        assert isinstance(MAX_DISCOVERY_DEPTH, int)
        assert (
            1 <= MAX_DISCOVERY_DEPTH <= 10
        ), "MAX_DISCOVERY_DEPTH must stay small — 6 is the slice UX-G choice"

    def test_max_sample_files_is_halved_from_original(self):
        """Slice UX-G lowered the per-run parsed-sample budget from 12 to 6."""
        assert MAX_SAMPLE_FILES <= 6, (
            "raising MAX_SAMPLE_FILES back to 12 re-introduces parse overhead "
            "that slice UX-G specifically removed"
        )

    def test_max_sample_rows_is_small(self):
        """The copilot only needs column names + inferred types; 5 sample
        rows is enough.  Larger values add IO+parse cost on every run."""
        assert MAX_SAMPLE_ROWS <= 10


# ---------------------------------------------------------------------------
# Discovery depth cap
# ---------------------------------------------------------------------------


class TestDiscoveryDepthCap:
    def test_walk_respects_depth_cap(self, tmp_path: Path):
        """A 10-level deep tree must not yield files from level 7+."""
        # Build a linear deep tree: root/d0/d1/.../d9/file.csv
        current = tmp_path
        for depth in range(10):
            current = current / f"d{depth}"
            current.mkdir()
            (current / f"file_{depth}.csv").write_text("col\n1\n", encoding="utf-8")

        files = list(_iter_candidate_files(tmp_path))
        # Every yielded file should be at depth <= MAX_DISCOVERY_DEPTH
        for f in files:
            parts = f.relative_to(tmp_path).parts
            file_depth = len(parts) - 1  # -1 because the file itself is not a dir
            assert file_depth <= MAX_DISCOVERY_DEPTH, (
                f"file {f.relative_to(tmp_path)} is at depth {file_depth}, "
                f"cap is {MAX_DISCOVERY_DEPTH}"
            )

    def test_walk_still_yields_top_level_files(self, tmp_path: Path):
        """Depth cap must not break the common case — files at the root."""
        (tmp_path / "contract.fluid.yaml").write_text("id: test", encoding="utf-8")
        (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
        (tmp_path / "data.csv").write_text("a,b\n1,2", encoding="utf-8")

        files = {f.name for f in _iter_candidate_files(tmp_path)}
        assert "contract.fluid.yaml" in files
        assert "README.md" in files
        assert "data.csv" in files

    def test_walk_respects_file_cap_after_depth_cap(self, tmp_path: Path):
        """Wide shallow tree still hits MAX_DISCOVERY_FILES."""
        # 500 CSVs at the root — exceeds MAX_DISCOVERY_FILES (300)
        for i in range(500):
            (tmp_path / f"file_{i:04d}.csv").write_text("a\n1\n", encoding="utf-8")
        files = list(_iter_candidate_files(tmp_path))
        assert (
            len(files) == MAX_DISCOVERY_FILES
        ), f"expected to be capped at {MAX_DISCOVERY_FILES}, got {len(files)}"

    def test_walk_skips_ignored_directories(self, tmp_path: Path):
        """Ignored dirs (.git, node_modules, .venv) must not be walked."""
        for ignored in (".git", "node_modules", ".venv", "__pycache__"):
            sub = tmp_path / ignored
            sub.mkdir()
            (sub / "should_not_appear.csv").write_text("x\n", encoding="utf-8")
        (tmp_path / "real.csv").write_text("x\n", encoding="utf-8")

        files = list(_iter_candidate_files(tmp_path))
        names = {f.name for f in files}
        assert "real.csv" in names
        assert "should_not_appear.csv" not in names


# ---------------------------------------------------------------------------
# Capability matrix cache
# ---------------------------------------------------------------------------


class TestCapabilityMatrixCache:
    def setup_method(self):
        clear_capability_matrix_cache()

    def teardown_method(self):
        clear_capability_matrix_cache()

    def test_first_call_returns_a_matrix(self):
        matrix = build_capability_matrix()
        assert isinstance(matrix, dict)
        assert "providers" in matrix
        assert "templates" in matrix
        assert "build_engines" in matrix

    def test_second_call_returns_equivalent_matrix(self):
        m1 = build_capability_matrix()
        m2 = build_capability_matrix()
        assert m1 == m2

    def test_caller_mutation_does_not_poison_cache(self):
        """The cache hands out deep copies so callers can freely
        mutate the result without affecting later calls."""
        m1 = build_capability_matrix()
        m1["providers"].append("evil-fake-provider")
        m1["templates"]["evil-fake-template"] = {"description": "injected"}

        m2 = build_capability_matrix()
        assert "evil-fake-provider" not in m2["providers"]
        assert "evil-fake-template" not in m2["templates"]

    def test_clear_cache_forces_rebuild(self):
        m1 = build_capability_matrix()
        # Force the next call to rebuild; result should still be equal
        # to the first one (the registry hasn't changed) but should be
        # a fresh object, not the same reference.
        clear_capability_matrix_cache()
        m2 = build_capability_matrix()
        assert m1 == m2
        assert m1 is not m2


# ---------------------------------------------------------------------------
# Sample-file schema cache
# ---------------------------------------------------------------------------


class TestSampleFileCache:
    def setup_method(self):
        clear_sample_file_cache()

    def teardown_method(self):
        clear_sample_file_cache()

    def test_first_and_second_calls_return_equivalent_result(self, tmp_path: Path):
        csv = tmp_path / "sample.csv"
        csv.write_text("id,name\n1,alice\n2,bob\n", encoding="utf-8")

        s1 = summarize_sample_file(csv)
        s2 = summarize_sample_file(csv)
        assert s1 == s2
        assert s1["columns"]  # non-empty

    def test_mutation_of_returned_dict_does_not_poison_cache(self, tmp_path: Path):
        csv = tmp_path / "sample.csv"
        csv.write_text("id,name\n1,alice\n", encoding="utf-8")

        s1 = summarize_sample_file(csv)
        s1["columns"]["evil_injected"] = "string"
        s1["warnings"] = ["fake warning"]

        s2 = summarize_sample_file(csv)
        assert "evil_injected" not in s2["columns"]
        assert s2.get("warnings", []) == [] or "fake warning" not in s2.get("warnings", [])

    def test_cache_invalidates_on_file_mtime_change(self, tmp_path: Path):
        import os
        import time

        csv = tmp_path / "sample.csv"
        csv.write_text("id,name\n1,alice\n", encoding="utf-8")
        s1 = summarize_sample_file(csv)
        assert "id" in s1["columns"]
        assert "extra" not in s1["columns"]

        # Bump mtime explicitly so the change is detectable even on
        # filesystems with coarse timestamp resolution.
        time.sleep(0.01)
        csv.write_text("id,name,extra\n1,alice,x\n", encoding="utf-8")
        new_mtime = time.time() + 1
        os.utime(csv, (new_mtime, new_mtime))

        s2 = summarize_sample_file(csv)
        assert "extra" in s2["columns"]

    def test_cache_invalidates_on_file_size_change(self, tmp_path: Path):
        """Even if two file versions share an mtime, a size change
        still invalidates the cache (size is part of the key)."""
        import os

        csv = tmp_path / "sample.csv"
        csv.write_text("id\n1\n", encoding="utf-8")
        s1 = summarize_sample_file(csv)
        assert "id" in s1["columns"]

        # Rewrite with a bigger body; pin mtime so only size changes.
        mtime = csv.stat().st_mtime
        csv.write_text("id,more_stuff\n1,alice\n2,bob\n", encoding="utf-8")
        os.utime(csv, (mtime, mtime))
        s2 = summarize_sample_file(csv)
        assert "more_stuff" in s2["columns"]

    def test_clear_sample_file_cache_is_a_noop_on_empty_cache(self):
        """Calling clear on an empty cache must not raise."""
        clear_sample_file_cache()  # No state — should just return.
        clear_sample_file_cache()  # Twice in a row for good measure.

    def test_unstattable_file_bypasses_cache(self, tmp_path: Path):
        """A file we can't stat returns a result (possibly with
        warnings) but the cache key is None, so the result is not
        memoized.  Calling summarize_sample_file twice on a missing
        file should simply hit the parse path twice without raising."""
        missing = tmp_path / "does-not-exist.csv"
        s1 = summarize_sample_file(missing)
        s2 = summarize_sample_file(missing)
        # Both calls return a dict shape (with warnings) without raising.
        assert isinstance(s1, dict)
        assert isinstance(s2, dict)
        assert s1.get("path") == str(missing)
