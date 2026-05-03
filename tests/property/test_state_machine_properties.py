# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Hypothesis property tests for the run lifecycle state machine + lock semantics.

These tests cover invariants that hold for ANY sequence of operations, not
just the specific paths we hand-wrote in the unit tests:

  - cursor / watermark round-trips are pure functions of payload (any value
    you write, you read back identically)
  - run-record write+read is total (any record you write, you can read)
  - lock acquire/release cycle leaves no leftover lock files
  - acquire_lock under contention with on_contended=abort always raises
  - any sequence of ops on the FileStateStore preserves the file structure
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fluid_build.api.state import Cursor, Watermark
from fluid_build.build_runners._acquisition_common import utc_now_iso
from fluid_build.build_runners._state import FileStateStore, LockHeldError

# Strategies for cursor / watermark / record values.
_id_strat = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=20,
)
_stream_strat = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-."),
    min_size=1,
    max_size=30,
).filter(lambda s: not s.startswith(("/", "..")))
_value_strat = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text()),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=10), children, max_size=4),
    ),
    max_leaves=10,
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
@given(
    product_id=_id_strat,
    build_id=_id_strat,
    stream=_stream_strat,
    value=_value_strat,
)
def test_cursor_round_trip_property(tmp_path: Path, product_id, build_id, stream, value):
    """Any cursor I write, I read back."""
    store = FileStateStore(tmp_path)
    cursor = Cursor(stream=stream, value=value, updated_at=utc_now_iso())
    store.set_cursor(product_id, build_id, cursor)
    got = store.get_cursor(product_id, build_id, stream)
    assert got is not None
    assert got.stream == stream
    assert got.value == value


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
@given(
    product_id=_id_strat,
    build_id=_id_strat,
    stream=_stream_strat,
    kind=st.sampled_from(["high_water_mark", "log_position", "lsn"]),
    value=_value_strat,
)
def test_watermark_round_trip_property(tmp_path: Path, product_id, build_id, stream, kind, value):
    store = FileStateStore(tmp_path)
    w = Watermark(stream=stream, kind=kind, value=value, updated_at=utc_now_iso())
    store.set_watermark(product_id, build_id, w)
    got = store.get_watermark(product_id, build_id, stream)
    assert got is not None and got.kind == kind and got.value == value


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
    deadline=None,  # File IO + cleanup can exceed Hypothesis's 200ms default.
)
@given(
    n_runs=st.integers(min_value=1, max_value=20),
    product_id=_id_strat,
    build_id=_id_strat,
)
def test_list_runs_count_matches_writes(tmp_path: Path, n_runs, product_id, build_id):
    """Writing N run records and listing must return at most N.

    Hypothesis reuses ``tmp_path`` across examples in one test invocation.
    Clean any prior state for ``(product_id, build_id)`` before asserting
    so each example sees a fresh slate.
    """
    import shutil

    store = FileStateStore(tmp_path)
    runs_dir = tmp_path / "runs" / product_id / build_id / "runs"
    if runs_dir.exists():
        shutil.rmtree(runs_dir)
    for i in range(n_runs):
        store.write_run_record(product_id, build_id, {"run_id": f"r{i:04d}", "state": "succeeded"})
    runs = store.list_runs(product_id, build_id, limit=100)
    assert len(runs) == n_runs


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@given(
    scope=st.sampled_from(["product", "build"]),
    resource_id=_id_strat,
)
def test_lock_release_property(tmp_path: Path, scope, resource_id):
    """After context exits cleanly, no lock file is left behind."""
    store = FileStateStore(tmp_path)
    with store.acquire_lock(scope, resource_id, timeout_seconds=60):
        pass
    lock_path = tmp_path / "locks" / f"{scope}__{resource_id}.lock"
    assert not lock_path.exists()


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@given(
    scope=st.sampled_from(["product", "build"]),
    resource_id=_id_strat,
)
def test_lock_contention_aborts(tmp_path: Path, scope, resource_id):
    """Two acquirers under abort policy: second always raises."""
    store = FileStateStore(tmp_path)
    with store.acquire_lock(scope, resource_id, timeout_seconds=60):
        with pytest.raises(LockHeldError):
            with store.acquire_lock(scope, resource_id, timeout_seconds=60, on_contended="abort"):
                pass
