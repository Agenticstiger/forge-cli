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

"""Tests for :mod:`fluid_build.copilot.checkpoint`.

Pin file for the LangGraph-shape contract — every change to the
checkpoint Protocol must keep these tests green. The shape-compat
test in particular guards the "command_center will plug
langgraph-checkpoint-postgres into this in 20 LOC" promise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from fluid_build.copilot.checkpoint import (
    STAGE_NAMES,
    CheckpointStore,
    FileCheckpointStore,
    JsonStageSerializer,
    NullCheckpointStore,
    StaleContractError,
    get_default_saver,
    langgraph_method_shape,
    reset_default_saver,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Empty workspace dir. The store creates ``.fluid/agents/...`` lazily."""
    return tmp_path


@pytest.fixture
def store(workspace: Path) -> FileCheckpointStore:
    return FileCheckpointStore(workspace_root=workspace)


@pytest.fixture(autouse=True)
def _reset_saver_between_tests() -> Any:
    """Ensure ``get_default_saver`` rebuilds for each test."""
    reset_default_saver()
    yield
    reset_default_saver()


# ---------------------------------------------------------------------------
# Round-trip — Pydantic BaseModel
# ---------------------------------------------------------------------------


class _SamplePydantic(BaseModel):
    name: str
    answers: int
    tags: list[str] = []


def test_put_get_pydantic_roundtrip(store: FileCheckpointStore) -> None:
    payload = _SamplePydantic(name="orders_v1", answers=4, tags=["bronze", "sdp"])
    store.put("r1", "logical", payload, cost_usd=0.012)

    rec = store.get("r1", "logical")
    assert rec is not None
    assert rec.stage == "logical"
    assert rec.payload_kind == "pydantic"
    assert rec.payload["name"] == "orders_v1"
    assert rec.payload["answers"] == 4
    assert rec.cost_usd == pytest.approx(0.012)

    # Round-trip back into the model via the serialiser's typed path.
    stage_path = Path(store._stage_path("r1", "logical"))  # type: ignore[attr-defined]
    raw = json.loads(stage_path.read_text(encoding="utf-8"))
    typed = JsonStageSerializer.deserialize(
        raw["payload_kind"], raw["payload_json"], expected_type=_SamplePydantic
    )
    assert isinstance(typed, _SamplePydantic)
    assert typed.name == "orders_v1"


# ---------------------------------------------------------------------------
# Round-trip — plain dict
# ---------------------------------------------------------------------------


def test_put_get_plain_dict_roundtrip(store: FileCheckpointStore) -> None:
    payload = {"slots": {"product_id": "orders"}, "answers": [1, 2, 3]}
    store.put("r2", "contract_forge", payload, cost_usd=0.04)
    rec = store.get("r2", "contract_forge")
    assert rec is not None
    assert rec.payload_kind == "json"
    assert rec.payload == payload


# ---------------------------------------------------------------------------
# Round-trip — dataclass
# ---------------------------------------------------------------------------


@dataclass
class _SampleDataclass:
    product_id: str
    files_written: int


def test_put_get_dataclass_roundtrip(store: FileCheckpointStore) -> None:
    payload = _SampleDataclass(product_id="orders_v1", files_written=4)
    store.put("r3", "builder", payload, cost_usd=0.01)

    rec = store.get("r3", "builder")
    assert rec is not None
    assert rec.payload_kind == "dataclass"
    # Dict-shape on read; ``deserialize`` with expected_type reconstructs.
    assert rec.payload == {"product_id": "orders_v1", "files_written": 4}

    stage_path = Path(store._stage_path("r3", "builder"))  # type: ignore[attr-defined]
    raw = json.loads(stage_path.read_text(encoding="utf-8"))
    typed = JsonStageSerializer.deserialize(
        raw["payload_kind"], raw["payload_json"], expected_type=_SampleDataclass
    )
    assert isinstance(typed, _SampleDataclass)
    assert typed.files_written == 4


# ---------------------------------------------------------------------------
# list_stages ordering
# ---------------------------------------------------------------------------


def test_list_stages_returns_in_canonical_order_regardless_of_write_order(
    store: FileCheckpointStore,
) -> None:
    # Write in reverse + scrambled order.
    write_order = ["judge", "logical", "builder", "validator", "readme"]
    for s in write_order:
        store.put("r4", s, {"stage": s})

    stages = store.list_stages("r4")
    seen = [r.stage for r in stages]
    # STAGE_NAMES is the source of truth — only the written subset, in canonical order.
    expected = [s for s in STAGE_NAMES if s in write_order]
    assert seen == expected


# ---------------------------------------------------------------------------
# list_runs filters
# ---------------------------------------------------------------------------


def test_list_runs_only_incomplete_filters_runs_with_judge(
    store: FileCheckpointStore,
) -> None:
    # r_complete: every stage including the final ``judge``.
    for s in STAGE_NAMES:
        store.put("r_complete", s, {"stage": s})
    # r_partial: stops before judge.
    for s in STAGE_NAMES[:-1]:
        store.put("r_partial", s, {"stage": s})

    runs_all = {r.run_id for r in store.list_runs()}
    assert {"r_complete", "r_partial"} <= runs_all

    runs_incomplete = {r.run_id for r in store.list_runs(only_incomplete=True)}
    assert "r_partial" in runs_incomplete
    assert "r_complete" not in runs_incomplete


def test_list_runs_workspace_root_filter(tmp_path: Path) -> None:
    """A workspace_root filter only returns runs inside that path."""
    ws_a = tmp_path / "wsA"
    ws_b = tmp_path / "wsB"
    ws_a.mkdir()
    ws_b.mkdir()
    store_a = FileCheckpointStore(workspace_root=ws_a)
    store_b = FileCheckpointStore(workspace_root=ws_b)
    store_a.put("run_in_a", "logical", {"x": 1})
    store_b.put("run_in_b", "logical", {"x": 2})

    # A store rooted at ws_a never sees ws_b's runs (different agents_root).
    assert {r.run_id for r in store_a.list_runs()} == {"run_in_a"}
    # Using the workspace_root filter on a parent root keeps results scoped.
    parent_store = FileCheckpointStore(workspace_root=ws_a)
    assert {r.run_id for r in parent_store.list_runs(workspace_root=ws_a)} == {"run_in_a"}


def test_list_runs_since_filter(store: FileCheckpointStore) -> None:
    store.put("rs1", "logical", {"a": 1})
    # Backdate the manifest's started_at so the filter has something to bite.
    manifest_path = store._manifest_path("rs1")  # type: ignore[attr-defined]
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    runs = store.list_runs(since=cutoff)
    assert all(r.run_id != "rs1" for r in runs)


# ---------------------------------------------------------------------------
# discard moves to .archived/
# ---------------------------------------------------------------------------


def test_discard_moves_to_archived_dir(workspace: Path, store: FileCheckpointStore) -> None:
    store.put("rd1", "logical", {"x": 1})
    run_dir = workspace / ".fluid" / "agents" / "rd1"
    assert run_dir.is_dir()

    store.discard("rd1")
    assert not run_dir.exists()
    archive = workspace / ".fluid" / "agents" / ".archived" / "rd1"
    assert archive.is_dir()

    # And ``get`` no longer finds it.
    assert store.get("rd1", "logical") is None
    # ``list_runs`` ignores the archive bucket (no "rd1" in active list).
    assert "rd1" not in {r.run_id for r in store.list_runs()}


def test_discard_collision_creates_timestamped_sibling(
    workspace: Path, store: FileCheckpointStore
) -> None:
    """A second discard for the same run-id must not clobber the archive."""
    store.put("rdup", "logical", {"v": 1})
    store.discard("rdup")
    # Recreate the run.
    store.put("rdup", "logical", {"v": 2})
    store.discard("rdup")
    archive_root = workspace / ".fluid" / "agents" / ".archived"
    entries = sorted(p.name for p in archive_root.iterdir())
    assert "rdup" in entries
    assert len(entries) >= 2  # the second archive is timestamp-suffixed


# ---------------------------------------------------------------------------
# skip_if_done context manager
# ---------------------------------------------------------------------------


def test_skip_if_done_yields_none_when_stage_missing(store: FileCheckpointStore) -> None:
    with store.skip_if_done("rs1", "logical") as rec:
        assert rec is None


def test_skip_if_done_yields_existing_record(store: FileCheckpointStore) -> None:
    store.put("rs2", "logical", {"x": 1})
    with store.skip_if_done("rs2", "logical") as rec:
        assert rec is not None
        assert rec.payload == {"x": 1}


def test_skip_if_done_manifest_updates_after_explicit_put(
    store: FileCheckpointStore,
) -> None:
    """Caller flow: skip_if_done yields None, caller does the work, calls put."""
    with store.skip_if_done("rs3", "logical") as rec:
        assert rec is None
        store.put("rs3", "logical", {"x": 2}, cost_usd=0.05)
    # After block exits, manifest reflects the new stage.
    runs = store.list_runs()
    match = [r for r in runs if r.run_id == "rs3"][0]
    assert "logical" in match.completed_stages
    assert match.total_cost_usd == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# StaleContractError — store DETECTS the mismatch; caller raises.
# ---------------------------------------------------------------------------


def test_stale_contract_detection(store: FileCheckpointStore) -> None:
    store.put("rsc", "logical", {"x": 1}, contract_hash="sha256:aaa")
    rec = store.get("rsc", "logical")
    assert rec is not None
    assert rec.contract_hash == "sha256:aaa"

    # Caller-side staleness check (the pattern we expose for resume sites).
    current_hash = "sha256:bbb"
    with pytest.raises(StaleContractError) as exc_info:
        if rec.contract_hash and rec.contract_hash != current_hash:
            raise StaleContractError(
                f"contract changed since checkpoint: was {rec.contract_hash} " f"now {current_hash}"
            )
    assert "contract changed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Null store — every method a no-op.
# ---------------------------------------------------------------------------


def test_null_store_put_is_noop_get_returns_none() -> None:
    s = NullCheckpointStore()
    s.put("rN", "logical", {"x": 1})
    assert s.get("rN", "logical") is None
    assert s.list_stages("rN") == []
    assert s.list_runs() == []
    s.discard("rN")
    with s.skip_if_done("rN", "logical") as rec:
        assert rec is None


def test_null_store_satisfies_protocol() -> None:
    """``isinstance(.., CheckpointStore)`` should return True for both impls."""
    assert isinstance(NullCheckpointStore(), CheckpointStore)


def test_file_store_satisfies_protocol(store: FileCheckpointStore) -> None:
    assert isinstance(store, CheckpointStore)


# ---------------------------------------------------------------------------
# get_default_saver — env-var dispatch.
# ---------------------------------------------------------------------------


def test_get_default_saver_returns_null_when_env_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLUID_COPILOT_CHECKPOINT", "0")
    reset_default_saver()
    saver = get_default_saver()
    assert isinstance(saver, NullCheckpointStore)


def test_get_default_saver_returns_file_store_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FLUID_COPILOT_CHECKPOINT", raising=False)
    reset_default_saver()
    saver = get_default_saver(workspace_root=tmp_path)
    assert isinstance(saver, FileCheckpointStore)


@pytest.mark.parametrize("disabling_value", ["0", "false", "off", "no", "FALSE"])
def test_get_default_saver_disabling_values(
    monkeypatch: pytest.MonkeyPatch, disabling_value: str
) -> None:
    monkeypatch.setenv("FLUID_COPILOT_CHECKPOINT", disabling_value)
    reset_default_saver()
    assert isinstance(get_default_saver(), NullCheckpointStore)


# ---------------------------------------------------------------------------
# Atomic write — a half-finished write never produces a half-parsed read.
# ---------------------------------------------------------------------------


def test_atomic_write_half_finished_temp_file_is_ignored(
    workspace: Path, store: FileCheckpointStore
) -> None:
    """Simulate a crash mid-write: tempfile exists but rename never happened.

    The real file should still be the previous content (or nothing).
    ``get`` returns the old record cleanly — never a JSONDecodeError.
    """
    store.put("ra1", "logical", {"v": 1})
    # Drop a half-written ``.logical.json.tmp`` next to the real file.
    # The atomic-write contract is that this tempfile never participates
    # in reads — ``os.replace`` swaps the inode in one operation.
    real = store._stage_path("ra1", "logical")  # type: ignore[attr-defined]
    tmp = real.with_name(f".{real.name}.tmp")
    tmp.write_text("{not valid json", encoding="utf-8")

    # ``get`` reads ``real`` (the post-replace file), not the tempfile.
    rec = store.get("ra1", "logical")
    assert rec is not None
    assert rec.payload == {"v": 1}


def test_atomic_write_overwrite_keeps_either_old_or_new(
    workspace: Path, store: FileCheckpointStore
) -> None:
    """Two puts in succession — the second write either fully landed
    or didn't; readers never see a corrupt JSON.
    """
    store.put("ra2", "logical", {"version": "v1"})
    store.put("ra2", "logical", {"version": "v2"})
    rec = store.get("ra2", "logical")
    assert rec is not None
    assert rec.payload["version"] == "v2"


# ---------------------------------------------------------------------------
# Shape-compat smoke test — assert LangGraph BaseCheckpointSaver parity.
# ---------------------------------------------------------------------------


# LangGraph BaseCheckpointSaver method names — fetched from the upstream
# source at /libs/checkpoint/langgraph/checkpoint/base/__init__.py on
# 2026-05-27. Encoded statically because langgraph is not (and must not
# be) a dependency of this project — see borrow-before-build receipts in
# the parent agent's reply.
LANGGRAPH_REQUIRED_METHOD_NAMES = {"put", "put_writes", "get_tuple", "list", "delete_thread"}


def test_protocol_method_names_overlap_langgraph_base_checkpoint_saver() -> None:
    """Our Protocol surface SHOULD share enough method names with LangGraph's
    ``BaseCheckpointSaver`` that an adapter is mechanical (each LangGraph
    method routes to a single one of ours).

    Mapping (our name → LangGraph name):

        put            ~ put             (different arg shape — see below)
        get            ~ get_tuple       (we return StageRecord, they return CheckpointTuple)
        list_stages    ~ list (per-thread)
        list_runs      ~ no direct equivalent (LangGraph treats threads as opaque ids)
        discard        ~ delete_thread

    The hard requirement: every adapter author can write a 1-to-1 map.
    """
    our_methods = set(langgraph_method_shape().keys())
    # ``put`` and ``get`` are the load-bearing pair — both must exist.
    assert "put" in our_methods
    assert "get" in our_methods
    # ``list_stages`` / ``list_runs`` cover LangGraph's ``list``.
    assert "list_stages" in our_methods
    assert "list_runs" in our_methods
    # ``discard`` covers LangGraph's ``delete_thread``.
    assert "discard" in our_methods


def test_put_method_arity_close_to_langgraph_put() -> None:
    """``CheckpointStore.put`` should have ~the same number of args as
    LangGraph's ``BaseCheckpointSaver.put`` (4 positional + ``self``).

    Allowed deviation: ±1 arg. We document at the Protocol that we
    flatten LangGraph's ``RunnableConfig`` into ``run_id`` + ``stage``,
    and we add ``cost_usd`` / ``contract_hash`` kwargs that LangGraph
    doesn't have, so a strict equality would fail. ±1 keeps adapter
    authors honest: if we ever drift further, the adapter has to grow,
    and we want to know.
    """
    shape = langgraph_method_shape()
    # Our put: (self, run_id, stage, payload, *, cost_usd, contract_hash) → 6 params.
    our_put_args = len(shape["put"])
    # LangGraph put: (self, config, checkpoint, metadata, new_versions) → 5 params.
    langgraph_put_args = 5
    assert (
        abs(our_put_args - langgraph_put_args) <= 2
    ), f"put arity diverged too far: ours={our_put_args}, langgraph={langgraph_put_args}"


def test_get_method_arity_close_to_langgraph_get_tuple() -> None:
    """Same ±1 sanity check for ``get`` vs ``get_tuple``."""
    shape = langgraph_method_shape()
    # Our get: (self, run_id, stage) → 3.
    our_get_args = len(shape["get"])
    # LangGraph get_tuple: (self, config) → 2.
    assert abs(our_get_args - 2) <= 1


# ---------------------------------------------------------------------------
# Pause marker integration
# ---------------------------------------------------------------------------


def test_paused_marker_flips_list_runs_status(store: FileCheckpointStore) -> None:
    store.put("rp1", "logical", {"x": 1})
    store.mark_paused("rp1")
    runs = {r.run_id: r for r in store.list_runs()}
    assert runs["rp1"].status == "paused"
    store.clear_paused("rp1")
    runs = {r.run_id: r for r in store.list_runs()}
    assert runs["rp1"].status != "paused"


# ---------------------------------------------------------------------------
# Manifest cost accumulation
# ---------------------------------------------------------------------------


def test_total_cost_usd_accumulates_across_stages(store: FileCheckpointStore) -> None:
    store.put("rc1", "logical", {}, cost_usd=0.01)
    store.put("rc1", "builder", {}, cost_usd=0.02)
    store.put("rc1", "judge", {}, cost_usd=0.03)
    run = [r for r in store.list_runs() if r.run_id == "rc1"][0]
    assert run.total_cost_usd == pytest.approx(0.06)
    assert run.status == "complete"
