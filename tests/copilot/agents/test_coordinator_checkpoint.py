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

"""StageCoordinator checkpoint integration pins.

The coordinator wraps each of the 8 stages in ``skip_if_done``:

    logical → contract_forge → builder ∥ readme ∥ transformation →
    validator → enrichment → judge

These tests use a recording mock store to assert which stages were
``skip_if_done``-wrapped, which were ``put``, and that the contract
hash flows from ``contract_forge`` onward to every later stage.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import yaml

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import StageCoordinator
from fluid_build.copilot.checkpoint import (
    STAGE_NAMES,
    JsonStageSerializer,
    NullCheckpointStore,
    StageRecord,
)
from fluid_build.copilot.schemas.intent import BusinessIntent, DataProduct, Dimensions, Grain
from fluid_build.copilot.schemas.stage_outputs import (
    PhysicalDraft,
    ReadmeDraft,
    TransformPlan,
    ValidationReport,
)
from fluid_build.copilot.store.backends.null import NullBackend

# ---------------------------------------------------------------------
# Mock store — records every interaction
# ---------------------------------------------------------------------


class RecordingCheckpointStore:
    """In-memory store that captures ``skip_if_done`` / ``put`` calls.

    Two private dicts:

    * ``records[(run_id, stage)]`` — the StageRecord-shaped dict last
      ``put`` for this key, or ``None`` if it was deleted.
    * ``skip_calls`` — list of ``(run_id, stage)`` tuples in the order
      ``skip_if_done`` was entered. Lets parallel tests assert "yes,
      we wrapped each parallel worker in its own context manager".
    """

    def __init__(self, prepopulated: Optional[Dict[Tuple[str, str], Any]] = None) -> None:
        self.records: Dict[Tuple[str, str], Any] = dict(prepopulated or {})
        self.skip_calls: List[Tuple[str, str]] = []
        self.put_calls: List[Tuple[str, str, Any, Optional[float], Optional[str]]] = []

    def put(
        self,
        run_id: str,
        stage: str,
        payload: Any,
        *,
        cost_usd: float = 0.0,
        contract_hash: Optional[str] = None,
    ) -> None:
        kind, text = JsonStageSerializer.serialize(payload)
        # Mirror the FileCheckpointStore's read-shape — payload is
        # always a dict when read back. Non-dict values get wrapped.
        try:
            import json

            decoded = json.loads(text)
        except Exception:
            decoded = payload
        if not isinstance(decoded, dict):
            decoded = {"value": decoded}
        record = StageRecord(
            run_id=run_id,
            stage=stage,
            completed_at="now",
            payload_kind=kind,
            payload=decoded,
            cost_usd=float(cost_usd or 0.0),
            contract_hash=contract_hash,
        )
        self.records[(run_id, stage)] = record
        self.put_calls.append((run_id, stage, payload, cost_usd, contract_hash))

    def get(self, run_id: str, stage: str) -> Optional[StageRecord]:
        return self.records.get((run_id, stage))

    def list_stages(self, run_id: str) -> list[StageRecord]:
        return [self.records[(run_id, s)] for s in STAGE_NAMES if (run_id, s) in self.records]

    def list_runs(self, **kwargs) -> list:
        return []

    def discard(self, run_id: str) -> None:
        for stage in list(STAGE_NAMES):
            self.records.pop((run_id, stage), None)

    @contextmanager
    def skip_if_done(self, run_id: str, stage: str):
        self.skip_calls.append((run_id, stage))
        yield self.records.get((run_id, stage))


# ---------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------


def _build_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer", "product"]),
    )


def _stub_physical_agents(monkeypatch) -> None:
    """Replace the three parallel physical agents + validator with
    cheap stubs. The coordinator's checkpointing happens around the
    agents — what the agents return is unimportant as long as the
    return shape is correct."""

    def fake_build_physical(self, sess, *, logical, contract, engine):
        return PhysicalDraft(
            contract=contract,
            logical=logical,
            transform_plan=TransformPlan(builds=[]),
            readme=ReadmeDraft(readme_markdown="builder-default"),
        )

    def fake_readme_run(self, logical, *, engine):
        return ReadmeDraft(readme_markdown="readme-stub")

    def fake_transformation_run(self, logical, *, engine):
        return TransformPlan(builds=[], additional_files={})

    def fake_validator_run(
        self, *, logical=None, contract=None, industry_pack=None, scratchpad=None
    ):
        return ValidationReport(score=10, issues=[], suggestions=[], passes_schema=True)

    monkeypatch.setattr(
        "fluid_build.copilot.agents.builder_agent.BuilderAgent.build_physical",
        fake_build_physical,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.readme_agent.ReadmeAgent.run",
        fake_readme_run,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.transformation_agent.TransformationAgent.run",
        fake_transformation_run,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.validator_agent.ValidatorAgent.run",
        fake_validator_run,
        raising=True,
    )


def _hash(contract: Dict[str, Any]) -> str:
    blob = yaml.safe_dump(contract, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Test 1 — every stage in the 8-stage pipeline calls skip_if_done
# ---------------------------------------------------------------------


def test_every_stage_wraps_in_skip_if_done(monkeypatch):
    """Bare-bones fan-out: every one of the 8 stages must surface in
    the recording store's ``skip_calls`` list. Coordinator-side wiring
    is the contract; what runs inside the context manager is the
    agents' concern."""
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
    monkeypatch.setenv("FLUID_COPILOT_POST_SYNTHESIS", "1")
    _stub_physical_agents(monkeypatch)

    store = RecordingCheckpointStore()
    session = StageSession(
        store=NullBackend(),
        capability_matrix={"critic_errors_trigger_repair": False},
    )
    coordinator = StageCoordinator(checkpoint_store=store)

    coordinator.from_intent(
        session,
        intent=_build_intent(),
        technique="dimensional",
        include_physical=True,
    )

    # The first six stages (logical → validator) ALWAYS surface.
    wrapped_stages = {stage for (_, stage) in store.skip_calls}
    for stage in (
        "logical",
        "contract_forge",
        "builder",
        "readme",
        "transformation",
        "validator",
    ):
        assert stage in wrapped_stages, f"stage {stage!r} not skip_if_done-wrapped"

    # Enrichment + judge run post-synthesis when physical landed.
    assert "enrichment" in wrapped_stages
    assert "judge" in wrapped_stages

    # Every stage was also ``put`` (since no cache pre-existed).
    put_stages = {stage for (_, stage, *_rest) in store.put_calls}
    assert {
        "logical",
        "contract_forge",
        "builder",
        "readme",
        "transformation",
        "validator",
        "enrichment",
        "judge",
    } <= put_stages


# ---------------------------------------------------------------------
# Test 2 — when every stage is checkpointed, fresh call uses cache
# ---------------------------------------------------------------------


def test_all_stages_cached_short_circuits_agents(monkeypatch):
    """Pre-populate the store with every stage's payload. The fresh
    ``from_intent`` call must return WITHOUT invoking any agent — the
    coordinator should pull straight from the cursors."""
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
    monkeypatch.setenv("FLUID_COPILOT_POST_SYNTHESIS", "1")

    # First — run once with stubs to populate the store. This run is
    # the "previous session" the resume picks up from.
    _stub_physical_agents(monkeypatch)
    store = RecordingCheckpointStore()
    session = StageSession(
        store=NullBackend(),
        capability_matrix={"critic_errors_trigger_repair": False},
    )
    coordinator = StageCoordinator(checkpoint_store=store)
    session.run_id = "run-cached"
    result1 = coordinator.from_intent(
        session,
        intent=_build_intent(),
        technique="dimensional",
        include_physical=True,
    )
    assert result1 is not None

    # Reset the put_calls list — we want to assert that the resume
    # run does NOT write anything new. (We can't assert "no agent
    # was called" directly without re-stubbing, so we assert the
    # weaker form: the resume hits skip_if_done on every stage and
    # the existing record path is taken.)
    store.put_calls.clear()
    store.skip_calls.clear()

    # Reset the coordinator's contract-hash cache so the resume run
    # actually picks up the stored hash rather than the in-memory one.
    coordinator._contract_hash = None
    coordinator._cost_at_last_checkpoint = 0.0

    # Replace all agent methods with explosions — any call means
    # the cache miss path was wrongly taken.
    def boom(*a, **kw):
        raise AssertionError("agent called on a fully-cached resume")

    monkeypatch.setattr(
        "fluid_build.copilot.agents.builder_agent.BuilderAgent.build_physical",
        boom,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.readme_agent.ReadmeAgent.run",
        boom,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.transformation_agent.TransformationAgent.run",
        boom,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.validator_agent.ValidatorAgent.run",
        boom,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.contract_forge_agent.ContractForgeAgent.forge_contract",
        boom,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.logical_agent.LogicalAgent.from_intent",
        boom,
        raising=True,
    )

    # The resume call should NOT invoke any agent.
    result2 = coordinator.from_intent(
        session,
        intent=_build_intent(),
        technique="dimensional",
        include_physical=True,
    )
    assert result2 is not None
    # No puts means no cache misses — every stage hit the cursor.
    assert store.put_calls == []


# ---------------------------------------------------------------------
# Test 3 — partial parallel: cached readme + missing transformation
# ---------------------------------------------------------------------


def test_partial_parallel_checkpoint_lets_other_workers_run(monkeypatch):
    """Pre-populate the readme cursor only. The next run must skip
    readme but still run builder + transformation. This is the
    LangGraph partial-super-step pattern — one of N parallel workers
    landed, the other two re-run."""
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
    monkeypatch.setenv("FLUID_COPILOT_POST_SYNTHESIS", "0")  # Skip enrichment/judge

    # Track which agents were actually called.
    called: List[str] = []

    def fake_build_physical(self, sess, *, logical, contract, engine):
        called.append("builder")
        return PhysicalDraft(
            contract=contract,
            logical=logical,
            transform_plan=TransformPlan(builds=[]),
            readme=ReadmeDraft(readme_markdown="builder-default"),
        )

    def fake_readme_run(self, logical, *, engine):
        called.append("readme")
        return ReadmeDraft(readme_markdown="readme-FRESH")

    def fake_transformation_run(self, logical, *, engine):
        called.append("transformation")
        return TransformPlan(builds=[], additional_files={"new_file": "yes"})

    def fake_validator_run(
        self, *, logical=None, contract=None, industry_pack=None, scratchpad=None
    ):
        called.append("validator")
        return ValidationReport(score=10, issues=[], suggestions=[], passes_schema=True)

    monkeypatch.setattr(
        "fluid_build.copilot.agents.builder_agent.BuilderAgent.build_physical",
        fake_build_physical,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.readme_agent.ReadmeAgent.run",
        fake_readme_run,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.transformation_agent.TransformationAgent.run",
        fake_transformation_run,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.validator_agent.ValidatorAgent.run",
        fake_validator_run,
        raising=True,
    )

    # Pre-populate ONLY the readme cursor.
    cached_readme = ReadmeDraft(readme_markdown="readme-FROM-CURSOR")
    store = RecordingCheckpointStore()
    # We pre-populate by calling put() directly so the store's record
    # shape matches what the coordinator would have written.
    store.put(
        "run-partial",
        "readme",
        cached_readme,
        cost_usd=0.01,
        contract_hash="prior-hash",
    )

    session = StageSession(
        store=NullBackend(),
        capability_matrix={"critic_errors_trigger_repair": False},
    )
    session.run_id = "run-partial"
    coordinator = StageCoordinator(checkpoint_store=store)

    result = coordinator.from_intent(
        session,
        intent=_build_intent(),
        technique="dimensional",
        include_physical=True,
    )

    # Readme was skipped (not in ``called``); builder + transformation
    # + validator ran fresh.
    assert "readme" not in called
    assert "builder" in called
    assert "transformation" in called
    # The restored readme is what flowed through.
    assert result.physical.readme.readme_markdown == "readme-FROM-CURSOR"


# ---------------------------------------------------------------------
# Test 4 — contract_hash flows from contract_forge onward
# ---------------------------------------------------------------------


def test_contract_hash_flows_from_contract_forge_onward(monkeypatch):
    """Once ``contract_forge`` lands, every later stage's checkpoint
    must carry the SAME contract_hash. ``logical`` legitimately has
    ``None`` (no contract yet)."""
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
    monkeypatch.setenv("FLUID_COPILOT_POST_SYNTHESIS", "1")
    _stub_physical_agents(monkeypatch)

    store = RecordingCheckpointStore()
    session = StageSession(
        store=NullBackend(),
        capability_matrix={"critic_errors_trigger_repair": False},
    )
    session.run_id = "run-hash"
    coordinator = StageCoordinator(checkpoint_store=store)

    coordinator.from_intent(
        session,
        intent=_build_intent(),
        technique="dimensional",
        include_physical=True,
    )

    # Pull every record from the store and inspect the contract_hash.
    hash_by_stage: Dict[str, Optional[str]] = {}
    for run_id, stage, _payload, _cost, contract_hash in store.put_calls:
        if run_id != "run-hash":
            continue
        hash_by_stage[stage] = contract_hash

    # Logical's hash is None.
    assert hash_by_stage["logical"] is None

    # Contract_forge stamped a non-None hash; every later stage carries
    # the SAME value.
    forge_hash = hash_by_stage["contract_forge"]
    assert forge_hash is not None and len(forge_hash) == 64  # sha256 hex

    for later_stage in (
        "builder",
        "readme",
        "transformation",
        "validator",
        "enrichment",
        "judge",
    ):
        assert hash_by_stage.get(later_stage) == forge_hash, (
            f"stage {later_stage!r} hash {hash_by_stage.get(later_stage)!r} "
            f"!= contract_forge hash {forge_hash!r}"
        )


# ---------------------------------------------------------------------
# Test 5 — FLUID_COPILOT_CHECKPOINT=0 → NullCheckpointStore is used
# ---------------------------------------------------------------------


def test_env_var_off_uses_null_store(monkeypatch, tmp_path):
    """Setting ``FLUID_COPILOT_CHECKPOINT=0`` must route the default
    saver to the no-op backend so no checkpoint files land on disk."""
    monkeypatch.setenv("FLUID_COPILOT_CHECKPOINT", "0")
    # Drop the singleton so the next get_default_saver() rebuilds.
    from fluid_build.copilot import checkpoint as ckpt_mod

    monkeypatch.setattr(ckpt_mod, "_DEFAULT_SAVER", None)

    # No explicit checkpoint_store kwarg → lazy resolve through the env
    # var → Null backend.
    coordinator = StageCoordinator()
    saver = coordinator._get_saver()
    assert isinstance(saver, NullCheckpointStore)

    # Run the coordinator. The Null backend should be the only one
    # used; no checkpoint files should land under tmp_path.
    _stub_physical_agents(monkeypatch)
    monkeypatch.setenv("FLUID_COPILOT_POST_SYNTHESIS", "0")
    session = StageSession(
        store=NullBackend(),
        capability_matrix={"critic_errors_trigger_repair": False},
    )
    session.run_id = "run-null"
    monkeypatch.chdir(tmp_path)
    coordinator.from_intent(
        session,
        intent=_build_intent(),
        technique="dimensional",
        include_physical=True,
    )

    # No .fluid/agents/run-null directory should have been written.
    agents_dir = tmp_path / ".fluid" / "agents" / "run-null"
    assert not agents_dir.exists()


# ---------------------------------------------------------------------
# Test 6 — checkpoint write failure does NOT poison a successful run
# ---------------------------------------------------------------------


def test_checkpoint_put_failure_is_swallowed(monkeypatch):
    """A store that raises on ``put`` must not break the forge. The
    coordinator logs at WARNING and continues. This guarantees the
    "checkpointing is orthogonal" invariant from the spec."""
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
    monkeypatch.setenv("FLUID_COPILOT_POST_SYNTHESIS", "0")
    _stub_physical_agents(monkeypatch)

    class ExplodingStore(RecordingCheckpointStore):
        def put(self, *a, **kw):
            raise RuntimeError("disk full")

    store = ExplodingStore()
    session = StageSession(
        store=NullBackend(),
        capability_matrix={"critic_errors_trigger_repair": False},
    )
    coordinator = StageCoordinator(checkpoint_store=store)

    # No exception should propagate.
    result = coordinator.from_intent(
        session,
        intent=_build_intent(),
        technique="dimensional",
        include_physical=True,
    )
    assert result is not None
    assert result.contract is not None
