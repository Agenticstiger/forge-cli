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

"""``MissionRunner`` — the VERIFY-anchored loop (deep-agents PR 2).

The tests that matter most here are the ones pinning the RFC's
non-negotiables:

- **Termination inversion** — an executor that loudly claims success
  while the on-disk contract still fails its checks must NOT end the
  mission; only the code-owned checks can.
- **Resume-by-re-verification** — a resumed run re-enters at VERIFY and
  terminates immediately if the contract already satisfies the criteria,
  with no replay of prior steps.
- **Fail-closed gate** — destructive diffs are refused on a non-TTY, and
  refused outright under ``gates.destructive: deny``.
- **Hard budgets** — iteration / wall-clock / USD ceilings each pause the
  run with the documented ``pause_reason``, never an invented status.

Everything below the runner runs for real: the actual check registry,
the actual predicate DSL, the actual on-disk read/write/hash cycle. Only
the LLM-facing collaborators (PLAN, EXECUTE) are injected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.copilot.missions.planner import MissionStep
from fluid_build.copilot.missions.runner import (
    STALL_PATIENCE,
    WALL_CLOCK_ENV,
    MissionOutcome,
    MissionRunner,
    run_mission,
)
from fluid_build.copilot.missions.spec import load_mission_spec_from_path
from fluid_build.copilot.missions.store import PAUSE_REASONS, RUN_STATUSES, MissionRunStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — a real contract that really fails a real criterion
# ---------------------------------------------------------------------------

BASE_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.2",
    "kind": "DataProduct",
    "id": "test.mission_target_v1",
    "name": "Mission Target",
    "domain": "test",
    "metadata": {
        "layer": "Bronze",
        "owner": {"team": "data-team", "email": "data@example.com"},
    },
    "exposes": [
        {
            "exposeId": "primary_output",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "csv",
                "location": {"path": "runtime/out/primary.csv"},
            },
            "contract": {
                "schema": [
                    {"name": "id", "type": "integer"},
                    {"name": "amount", "type": "number"},
                ]
            },
        }
    ],
}

SPEC_YAML = """\
name: test-coverage
description: Test mission.
goal: Every output port carries at least one data-quality rule.
success_criteria:
  - check: predicate
    path: "exposes[*].contract.dq.rules"
    op: exists
budgets:
  max_usd: 5.0
  max_iterations: 3
  max_wall_seconds: 600
gates:
  destructive: ask
tools:
  allow: [validate_contract, propose_contract]
plan_hint: [add_dq_rules]
"""


def _write_contract(path: Path, contract: Dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")


def _with_dq_rules(contract: Dict[str, Any]) -> Dict[str, Any]:
    """The edit that actually satisfies the criterion."""
    import copy

    fixed = copy.deepcopy(contract)
    fixed["exposes"][0]["contract"]["dq"] = {
        "rules": [{"name": "id_not_null", "type": "notNull", "column": "id"}]
    }
    return fixed


@pytest.fixture()
def spec(tmp_path):
    spec_path = tmp_path / "test_coverage.yaml"
    spec_path.write_text(SPEC_YAML, encoding="utf-8")
    return load_mission_spec_from_path(spec_path)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FLUID_USER_HOME", str(tmp_path / "home"))
    # Never let an ambient operator ceiling leak into budget assertions.
    monkeypatch.delenv("FLUID_COST_LIMIT_USD_PER_PRODUCT", raising=False)
    monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
    monkeypatch.delenv(WALL_CLOCK_ENV, raising=False)
    return ws


@pytest.fixture()
def contract_path(workspace):
    path = workspace / "contract.fluid.yaml"
    _write_contract(path, BASE_CONTRACT)
    return path


def _plan_one(*_args, **_kwargs) -> List[MissionStep]:
    return [MissionStep(action="edit_contract", goal="add dq rules")]


def _runner(spec, contract_path, workspace, **overrides) -> MissionRunner:
    kwargs: Dict[str, Any] = {
        "workspace_root": workspace,
        "plan_fn": _plan_one,
        "confirm_fn": lambda *a, **k: False,
        "llm_config": None,
    }
    kwargs.update(overrides)
    return MissionRunner(spec, contract_path, **kwargs)


# ---------------------------------------------------------------------------
# Termination inversion — the load-bearing property
# ---------------------------------------------------------------------------


def test_llm_cannot_declare_success_when_checks_still_fail(spec, contract_path, workspace):
    """An executor that returns a 'done' claim but no real fix cannot win.

    The fake executor here echoes the contract back untouched while
    asserting completion in its payload. The mission must run to its
    iteration cap and pause — the scorecard, not the model, decides.
    """

    def _lying_executor(step, contract, **kwargs):
        # Byte-identical echo: nothing actually changed on disk.
        return dict(contract)

    outcome = _runner(spec, contract_path, workspace, execute_fn=_lying_executor).run()

    assert outcome.status == "paused"
    assert outcome.pause_reason in PAUSE_REASONS
    assert outcome.scorecard is not None
    assert outcome.scorecard.passed is False


def test_mission_completes_only_when_the_code_owned_check_passes(spec, contract_path, workspace):
    def _real_executor(step, contract, **kwargs):
        return _with_dq_rules(contract)

    outcome = _runner(spec, contract_path, workspace, execute_fn=_real_executor).run()

    assert outcome.status == "complete"
    assert outcome.passed is True
    assert outcome.pause_reason is None
    assert outcome.scorecard.passed is True
    # And the win is anchored to the file, not the in-memory dict.
    on_disk = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert on_disk["exposes"][0]["contract"]["dq"]["rules"]


def test_verify_reads_the_contract_from_disk_each_cycle(spec, contract_path, workspace):
    """A contract fixed out-of-band completes without any executor work."""
    _write_contract(contract_path, _with_dq_rules(BASE_CONTRACT))

    def _never_called(step, contract, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("EXECUTE ran even though VERIFY already passed")

    outcome = _runner(spec, contract_path, workspace, execute_fn=_never_called).run()
    assert outcome.status == "complete"
    assert outcome.cycles == 1


# ---------------------------------------------------------------------------
# Resume — idempotent VERIFY is the whole mechanism
# ---------------------------------------------------------------------------


def test_resume_re_enters_at_verify_with_no_replay(spec, contract_path, workspace):
    """A paused run resumes by re-verifying, not by replaying steps."""

    def _noop_executor(step, contract, **kwargs):
        return dict(contract)

    first = _runner(spec, contract_path, workspace, execute_fn=_noop_executor)
    paused = first.run()
    assert paused.status == "paused"

    # Operator fixes the contract by hand between the two invocations.
    _write_contract(contract_path, _with_dq_rules(BASE_CONTRACT))

    def _must_not_run(step, contract, **kwargs):  # pragma: no cover
        raise AssertionError("resume replayed a step instead of re-verifying")

    resumed = MissionRunner.resume(
        spec,
        contract_path,
        workspace_root=workspace,
        plan_fn=_plan_one,
        execute_fn=_must_not_run,
        confirm_fn=lambda *a, **k: False,
        llm_config=None,
    ).run()

    assert resumed.status == "complete"
    assert resumed.run_id == paused.run_id  # same run dir, not a new one
    assert resumed.cycles == 1


def test_completed_missions_do_not_linger_as_resumable(spec, contract_path, workspace):
    _write_contract(contract_path, _with_dq_rules(BASE_CONTRACT))
    done = _runner(spec, contract_path, workspace, execute_fn=lambda *a, **k: None).run()
    assert done.status == "complete"

    from fluid_build.copilot.missions.store import find_resumable_run

    assert find_resumable_run(workspace, mission=spec.name) is None


def test_manifest_status_stays_inside_the_documented_literal_set(spec, contract_path, workspace):
    outcome = _runner(spec, contract_path, workspace, execute_fn=lambda s, c, **k: dict(c)).run()
    manifest = json.loads((outcome.run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["status"] in RUN_STATUSES
    assert manifest["status"] == "paused"
    assert manifest["pause_reason"] in PAUSE_REASONS
    # Additive mission fields ride alongside the checkpoint-shaped ones.
    assert manifest["mission"] == spec.name
    assert manifest["mission_spec_sha256"] == spec.content_sha256
    assert manifest["criteria_status"]["total"] == 1


def test_scorecard_is_digest_bound_to_the_verified_contract(spec, contract_path, workspace):
    outcome = _runner(spec, contract_path, workspace, execute_fn=lambda s, c, **k: dict(c)).run()
    scorecard = json.loads((outcome.run_dir / "scorecard.json").read_text(encoding="utf-8"))

    from fluid_build.copilot.missions.checks import load_contract_for_checks

    _, current_hash = load_contract_for_checks(contract_path)
    assert scorecard["contract_sha256"] == current_hash
    assert scorecard["run_id"] == outcome.run_id


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_iteration_cap_pauses_with_reason_iterations(tmp_path, contract_path, workspace):
    """The cap fires on its own when the run is too short to look stalled.

    ``max_iterations: 2`` keeps the progress history inside the stall
    patience window, so ``iterations`` is the only ceiling that can fire.
    """
    spec_path = tmp_path / "short.yaml"
    spec_path.write_text(SPEC_YAML.replace("max_iterations: 3", "max_iterations: 2"), "utf-8")
    short_spec = load_mission_spec_from_path(spec_path)

    outcome = _runner(short_spec, contract_path, workspace, execute_fn=lambda s, c, **k: None).run()
    assert outcome.status == "paused"
    assert outcome.pause_reason == "iterations"
    assert outcome.cycles == 2


def test_a_plateauing_run_pauses_as_stalled_before_the_cap(spec, contract_path, workspace):
    """Stall is the more informative reason when both ceilings are in reach."""
    outcome = _runner(spec, contract_path, workspace, execute_fn=lambda s, c, **k: None).run()
    assert outcome.status == "paused"
    assert outcome.pause_reason == "stalled"


def test_wall_clock_deadline_pauses_with_reason_timeout(
    spec, contract_path, workspace, monkeypatch
):
    monkeypatch.setenv(WALL_CLOCK_ENV, "30")
    clock = {"t": 0.0}

    def _now():
        clock["t"] += 20.0  # blow past the 30s budget on the second probe
        return clock["t"]

    outcome = _runner(
        spec,
        contract_path,
        workspace,
        execute_fn=lambda s, c, **k: dict(c),
        now_fn=_now,
    ).run()
    assert outcome.status == "paused"
    assert outcome.pause_reason == "timeout"


def test_usd_ceiling_pauses_with_reason_budget(spec, contract_path, workspace, monkeypatch):
    """Spend is re-summed from on-disk receipts, so a resumed run inherits it."""
    runner = _runner(spec, contract_path, workspace, execute_fn=lambda s, c, **k: dict(c))
    # Pre-seed a receipt that already exhausts the spec's $5 budget —
    # exactly the shape a prior (paused) run would have left behind.
    store = MissionRunStore(workspace, runner.store.run_id)
    receipt = store.cycle_dir(1) / "cost.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"total_usd": 9.99}), encoding="utf-8")

    outcome = runner.run()
    assert outcome.status == "paused"
    assert outcome.pause_reason == "budget"
    assert outcome.spend_usd >= 9.99


def test_spend_re_sums_across_receipts(workspace):
    store = MissionRunStore(workspace, "run-abc")
    for cycle, usd in ((1, 0.25), (2, 0.5)):
        path = store.cycle_dir(cycle) / "cost.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"total_usd": usd}), encoding="utf-8")
    assert store.spend_from_receipts() == pytest.approx(0.75)


def test_remaining_wall_time_becomes_the_per_call_timeout(
    spec, contract_path, workspace, monkeypatch
):
    """The deadline is pushed down as the LLM call timeout, not just checked."""
    import dataclasses

    monkeypatch.setenv(WALL_CLOCK_ENV, "40")

    @dataclasses.dataclass
    class _Cfg:
        provider: str = "gemini"
        model: str = "gemini-2.5-flash"
        timeout_seconds: int = 120

    runner = _runner(
        spec,
        contract_path,
        workspace,
        llm_config=_Cfg(),
        execute_fn=lambda s, c, **k: None,
    )
    runner._deadline = runner._now() + 40
    scoped = runner._call_llm_config()
    assert scoped.timeout_seconds <= 40
    assert scoped.timeout_seconds < _Cfg().timeout_seconds


def test_stall_detection_pauses_when_progress_plateaus(spec, contract_path, workspace):
    runner = _runner(spec, contract_path, workspace, execute_fn=lambda s, c, **k: None)
    # Flat history longer than the patience window is a stall.
    assert runner._stalled([1] * (STALL_PATIENCE + 1)) is True
    # Strictly increasing is never a stall.
    assert runner._stalled(list(range(STALL_PATIENCE + 2))) is False
    # Too-short history cannot conclude anything yet.
    assert runner._stalled([0]) is False


# ---------------------------------------------------------------------------
# GATE — fail closed
# ---------------------------------------------------------------------------


def _deleting_executor(step, contract, **kwargs):
    """The Goodhart attack: satisfy the criterion by deleting the port."""
    stripped = dict(contract)
    stripped["exposes"] = []
    return stripped


def test_destructive_edit_is_blocked_and_never_written(spec, contract_path, workspace):
    before = contract_path.read_text(encoding="utf-8")
    outcome = _runner(
        spec,
        contract_path,
        workspace,
        execute_fn=_deleting_executor,
        confirm_fn=lambda *a, **k: False,  # non-TTY posture
    ).run()

    assert outcome.status == "paused"
    assert contract_path.read_text(encoding="utf-8") == before
    kinds = {e["event"] for e in outcome.events}
    assert "mission_destructive_gate_rejected" in kinds


def test_gates_destructive_deny_refuses_without_prompting(tmp_path, contract_path, workspace):
    spec_path = tmp_path / "deny.yaml"
    spec_path.write_text(SPEC_YAML.replace("destructive: ask", "destructive: deny"), "utf-8")
    denied_spec = load_mission_spec_from_path(spec_path)

    def _confirm_must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError("deny mode prompted instead of refusing outright")

    outcome = _runner(
        denied_spec,
        contract_path,
        workspace,
        execute_fn=_deleting_executor,
        confirm_fn=_confirm_must_not_run,
    ).run()
    assert outcome.status == "paused"
    assert any(
        e.get("reason") == "gates_destructive_deny"
        for e in outcome.events
        if e["event"] == "mission_destructive_gate_rejected"
    )


def test_confirm_fail_closed_rejects_without_a_tty(monkeypatch, caplog):
    from fluid_build.copilot.missions import gate

    monkeypatch.setattr(gate, "_stdin_is_tty", lambda: False)
    with caplog.at_level("WARNING"):
        approved = gate.confirm_fail_closed(["exposes: removed"], mission="m", step="s")
    assert approved is False
    assert gate.GATE_REJECTED_EVENT in caplog.text


def test_confirm_fail_closed_has_no_auto_yes_channel():
    """``--yes`` structurally cannot reach the destructive gate."""
    import inspect

    from fluid_build.copilot.missions.gate import confirm_fail_closed

    params = set(inspect.signature(confirm_fail_closed).parameters)
    assert "auto_yes" not in params
    assert "yes" not in params


def test_confirm_fail_closed_requires_an_explicit_yes():
    from fluid_build.copilot.missions.gate import confirm_fail_closed

    assert confirm_fail_closed([], input_fn=lambda _: "y", printer=lambda _: None) is True
    assert confirm_fail_closed([], input_fn=lambda _: "", printer=lambda _: None) is False
    assert confirm_fail_closed([], input_fn=lambda _: "sure", printer=lambda _: None) is False


def test_approved_destructive_edit_does_land(spec, contract_path, workspace):
    """The gate is a gate, not a wall — an explicit yes still writes."""
    outcome = _runner(
        spec,
        contract_path,
        workspace,
        execute_fn=_deleting_executor,
        confirm_fn=lambda *a, **k: True,
    ).run()
    on_disk = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert on_disk["exposes"] == []
    assert outcome.status == "paused"  # deleting the port doesn't satisfy the check


# ---------------------------------------------------------------------------
# Concurrency + robustness
# ---------------------------------------------------------------------------


def test_out_of_band_contract_change_abandons_the_step(spec, contract_path, workspace):
    """A contract that moves mid-step re-enters VERIFY instead of clobbering."""
    seen: List[str] = []

    def _slow_executor(step, contract, **kwargs):
        seen.append("executed")
        # Simulate a concurrent editor saving the file mid-step.
        _write_contract(contract_path, _with_dq_rules(BASE_CONTRACT))
        return dict(contract)  # a stale proposal built from the old read

    outcome = _runner(spec, contract_path, workspace, execute_fn=_slow_executor).run()

    assert seen  # the step did run
    assert outcome.status == "complete"  # the out-of-band fix is what VERIFY saw
    assert any(e["event"] == "mission_contract_changed_out_of_band" for e in outcome.events)


def test_executor_exception_does_not_end_the_mission_or_leak_text(spec, contract_path, workspace):
    def _exploding_executor(step, contract, **kwargs):
        raise RuntimeError("s3://bucket?key=AKIAIOSFODNN7EXAMPLE leaked secret")

    outcome = _runner(spec, contract_path, workspace, execute_fn=_exploding_executor).run()

    assert outcome.status == "paused"  # ran to the cap, not crashed
    failures = [e for e in outcome.events if e["event"] == "mission_step_failed"]
    assert failures and failures[0]["error"] == "RuntimeError"
    # Typed name only — the exception's text never enters the event stream.
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(outcome.to_dict())


def test_missing_contract_fails_rather_than_pausing(spec, workspace):
    outcome = MissionRunner(
        spec,
        workspace / "does-not-exist.yaml",
        workspace_root=workspace,
        plan_fn=_plan_one,
        execute_fn=lambda *a, **k: None,
        llm_config=None,
    ).run()
    assert outcome.status == "failed"
    assert "unreadable" in outcome.detail


def test_run_mission_helper_returns_an_outcome(spec, contract_path, workspace):
    _write_contract(contract_path, _with_dq_rules(BASE_CONTRACT))
    outcome = run_mission(
        spec,
        contract_path,
        workspace_root=workspace,
        plan_fn=_plan_one,
        execute_fn=lambda *a, **k: None,
        llm_config=None,
    )
    assert isinstance(outcome, MissionOutcome)
    assert outcome.status == "complete"


# ---------------------------------------------------------------------------
# PLAN
# ---------------------------------------------------------------------------


def test_planner_falls_back_deterministically_when_the_llm_fails(spec, contract_path):
    from fluid_build.copilot.missions.checks import run_mission_checks
    from fluid_build.copilot.missions.planner import plan_steps

    scorecard = run_mission_checks(spec, contract_path)

    def _boom(*_a, **_k):
        raise RuntimeError("provider down")

    steps = plan_steps(spec, scorecard, llm_config=None, call_llm_fn=_boom, provider=object())
    assert len(steps) == 1
    assert steps[0].action == "edit_contract"
    # The fallback recycles the failing diagnostics as the repair prompt.
    assert "dq" in steps[0].goal


def test_planner_collapses_unknown_actions_to_edit_contract(spec, contract_path):
    from fluid_build.copilot.missions.checks import run_mission_checks
    from fluid_build.copilot.missions.planner import plan_steps

    scorecard = run_mission_checks(spec, contract_path)
    payload = json.dumps(
        {
            "steps": [
                {"action": "rm -rf /", "goal": "delete everything"},
                {"action": "enforce_ai_ready", "goal": "annotate"},
            ]
        }
    )
    steps = plan_steps(
        spec, scorecard, llm_config=None, call_llm_fn=lambda *a, **k: payload, provider=object()
    )
    assert [s.action for s in steps] == ["edit_contract", "enforce_ai_ready"]
    assert steps[1].deterministic is True


def test_plan_prompt_never_claims_the_llm_can_finish(spec, contract_path):
    from fluid_build.copilot.missions.checks import run_mission_checks
    from fluid_build.copilot.missions.planner import build_plan_prompt

    prompt = build_plan_prompt(spec, run_mission_checks(spec, contract_path))
    assert "only the checks decide" in prompt
    assert "Do not add tools" in prompt


# ---------------------------------------------------------------------------
# Destructive classifier — the fail-closed matrix
# ---------------------------------------------------------------------------


def _diff(old: Dict[str, Any], new: Dict[str, Any]):
    from fluid_build.copilot.missions.destructive import classify_contract_diff

    return classify_contract_diff(old, new)


@pytest.mark.parametrize(
    "mutate, expected_destructive, why",
    [
        (lambda c: c, False, "no change at all"),
        (
            lambda c: {**c, "exposes": []},
            True,
            "output port removed",
        ),
        (
            lambda c: {**c, "description": "added"},
            False,
            "new top-level key is additive",
        ),
        (
            lambda c: {k: v for k, v in c.items() if k != "domain"},
            True,
            "key removed",
        ),
    ],
)
def test_destructive_matrix_top_level(mutate, expected_destructive, why):
    import copy

    old = copy.deepcopy(BASE_CONTRACT)
    new = mutate(copy.deepcopy(BASE_CONTRACT))
    assert _diff(old, new).destructive is expected_destructive, why


def test_adding_dq_rules_is_not_destructive():
    assert _diff(BASE_CONTRACT, _with_dq_rules(BASE_CONTRACT)).destructive is False


def test_removing_a_schema_column_is_destructive():
    import copy

    new = copy.deepcopy(BASE_CONTRACT)
    new["exposes"][0]["contract"]["schema"].pop()
    verdict = _diff(BASE_CONTRACT, new)
    assert verdict.destructive is True
    assert any(f.kind == "removal" for f in verdict.destructive_findings)


def test_narrowing_a_column_type_is_destructive():
    import copy

    new = copy.deepcopy(BASE_CONTRACT)
    new["exposes"][0]["contract"]["schema"][1]["type"] = "integer"
    verdict = _diff(BASE_CONTRACT, new)
    assert verdict.destructive is True
    assert any(f.kind == "type_change" for f in verdict.destructive_findings)


def test_editing_a_description_is_not_destructive():
    import copy

    new = copy.deepcopy(BASE_CONTRACT)
    new["exposes"][0]["contract"]["schema"][0]["description"] = "the row id"
    assert _diff(BASE_CONTRACT, new).destructive is False


def test_growing_a_retention_window_is_destructive_but_shrinking_is_not():
    old = {"policy": {"maxRetentionDays": 30}}
    assert _diff(old, {"policy": {"maxRetentionDays": 90}}).destructive is True
    assert _diff(old, {"policy": {"maxRetentionDays": 7}}).destructive is False


def test_widening_an_allowlist_is_destructive():
    old = {"policy": {"allowedModels": ["gpt-4"]}}
    new = {"policy": {"allowedModels": ["gpt-4", "anything-goes"]}}
    assert _diff(old, new).destructive is True


def test_unrecognised_value_change_fails_closed():
    """The whole point: a shape we have no rule for is destructive."""
    verdict = _diff({"someNovelKnob": "safe"}, {"someNovelKnob": "unsafe"})
    assert verdict.destructive is True
    assert "fail-closed" in verdict.destructive_findings[0].detail


def test_container_shape_change_fails_closed():
    assert _diff({"exposes": []}, {"exposes": {}}).destructive is True


def test_a_missing_new_contract_is_a_wipe():
    assert _diff(BASE_CONTRACT, None).destructive is True


def test_volatile_provenance_is_ignored_by_the_diff():
    """Re-stamped bookkeeping must not gate every single cycle."""
    import copy

    old = copy.deepcopy(BASE_CONTRACT)
    old["metadata"]["provenance"] = {"generated_at": "2026-01-01T00:00:00Z"}
    new = copy.deepcopy(BASE_CONTRACT)  # the LLM's echo drops provenance
    assert _diff(old, new).destructive is False


# ---------------------------------------------------------------------------
# Inner-loop surgery — additive parameters, default behaviour unchanged
# ---------------------------------------------------------------------------


def test_tool_allowlist_defaults_to_the_full_registry():
    from fluid_build.cli.forge_copilot_agent_loop import _filter_tools

    tools = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert _filter_tools(tools, None) is tools
    assert _filter_tools(tools, []) is tools


def test_tool_allowlist_intersects_with_the_registry():
    from fluid_build.cli.forge_copilot_agent_loop import _filter_tools

    tools = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert [t["name"] for t in _filter_tools(tools, ["a", "c"])] == ["a", "c"]
    # Names that aren't real tools are ignored, not fatal.
    assert [t["name"] for t in _filter_tools(tools, ["a", "nope"])] == ["a"]


def test_tool_allowlist_that_matches_nothing_falls_back_loudly(caplog):
    from fluid_build.cli.forge_copilot_agent_loop import _filter_tools

    tools = [{"name": "a"}]
    with caplog.at_level("WARNING"):
        result = _filter_tools(tools, ["totally-unknown"])
    assert result is tools
    assert "matched no registered tools" in caplog.text


def test_goal_scope_defaults_to_an_unchanged_system_prompt():
    from fluid_build.cli.forge_copilot_agent_loop import _scoped_system_prompt

    assert _scoped_system_prompt("BASE", None) == "BASE"
    assert _scoped_system_prompt("BASE", "") == "BASE"


def test_goal_scope_forbids_deletion_and_denies_the_model_a_verdict():
    from fluid_build.cli.forge_copilot_agent_loop import _scoped_system_prompt

    prompt = _scoped_system_prompt("BASE", "add dq rules")
    assert "add dq rules" in prompt
    assert "Do NOT delete fields" in prompt
    assert "You do not decide whether this step succeeded" in prompt


def test_seed_context_uses_the_existing_override_seam():
    from fluid_build.cli.forge_copilot_runtime import build_agent_loop_seed_context

    enriched = build_agent_loop_seed_context({"project_goal": "g"}, seed_contract=BASE_CONTRACT)
    assert enriched["seed_contract_override"]["id"] == BASE_CONTRACT["id"]
    assert enriched["project_goal"] == "g"


def test_seed_context_rejects_a_non_contract_and_is_inert_without_a_seed():
    from fluid_build.cli.forge_copilot_runtime import build_agent_loop_seed_context

    assert "seed_contract_override" not in build_agent_loop_seed_context({}, seed_contract=None)
    assert "seed_contract_override" not in build_agent_loop_seed_context(
        {}, seed_contract={"kind": "NotAContract"}
    )


def test_seeded_initial_message_asks_for_an_edit_not_an_authoring_run():
    from fluid_build.cli.forge_copilot_agent_loop import _build_initial_user_message

    seeded = _build_initial_user_message(
        {"project_goal": "g", "seed_contract_override": BASE_CONTRACT}
    )
    assert "EXISTING CONTRACT" in seeded
    assert BASE_CONTRACT["id"] in seeded

    plain = _build_initial_user_message({"project_goal": "g"})
    assert "EXISTING CONTRACT" not in plain
    assert "discover my workspace" in plain


# ---------------------------------------------------------------------------
# Gate baseline anchoring — found by a live run, pinned here
# ---------------------------------------------------------------------------


FULL_SPEC_YAML = SPEC_YAML.replace(
    "success_criteria:\n", "success_criteria:\n  - check: validate\n"
)


@pytest.fixture()
def full_spec(tmp_path):
    """validate + predicate — the shape the shipped quality-coverage uses."""
    spec_path = tmp_path / "full_coverage.yaml"
    spec_path.write_text(FULL_SPEC_YAML, encoding="utf-8")
    return load_mission_spec_from_path(spec_path)


def test_mission_may_revise_its_own_earlier_edit(full_spec, contract_path, workspace):
    """The gate must not protect the model's bad output from its own repair.

    Live-run pathology this pins: cycle 1 wrote a malformed ``dq.rules``
    block; every cycle-2 correction was rejected as "destructive",
    because fixing a malformed block means removing malformed keys. With
    the diff anchored at mission start, mission-authored content is
    revisable and the run converges.
    """
    calls = {"n": 0}

    def _two_stage_executor(step, contract, **kwargs):
        import copy

        calls["n"] += 1
        proposed = copy.deepcopy(contract)
        if calls["n"] == 1:
            # A malformed first attempt — extra keys the schema hates.
            proposed["exposes"][0]["contract"]["dq"] = {
                "rules": [{"name": "bad", "column": "id", "type": "not_null"}]
            }
        else:
            # The correction: drops the bad keys entirely.
            proposed["exposes"][0]["contract"]["dq"] = {
                "rules": [{"id": "r1", "type": "completeness", "severity": "error"}]
            }
        return proposed

    outcome = _runner(
        full_spec,
        contract_path,
        workspace,
        execute_fn=_two_stage_executor,
        confirm_fn=lambda *a, **k: False,  # non-TTY: nothing destructive can land
    ).run()

    assert calls["n"] >= 2, "the second, corrective edit never ran"
    on_disk = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    rules = on_disk["exposes"][0]["contract"]["dq"]["rules"]
    assert rules[0]["type"] == "completeness", "the correction was blocked by the gate"
    assert "column" not in rules[0]
    assert outcome.status == "complete"


def test_pre_existing_content_stays_protected_across_cycles(spec, contract_path, workspace):
    """Baseline anchoring must not weaken protection of the operator's data."""

    def _deletes_on_the_second_cycle(step, contract, **kwargs):
        import copy

        proposed = copy.deepcopy(contract)
        # Add the rules (safe) AND delete an original column (not safe).
        proposed["exposes"][0]["contract"]["dq"] = {"rules": [{"id": "r", "type": "completeness"}]}
        proposed["exposes"][0]["contract"]["schema"].pop()
        return proposed

    before = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    outcome = _runner(
        spec,
        contract_path,
        workspace,
        execute_fn=_deletes_on_the_second_cycle,
        confirm_fn=lambda *a, **k: False,
    ).run()

    after = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert after["exposes"][0]["contract"]["schema"] == before["exposes"][0]["contract"]["schema"]
    assert any(e["event"] == "mission_destructive_gate_rejected" for e in outcome.events)


def test_extract_proposed_contract_accepts_both_envelope_shapes():
    """Live models return the bare contract as often as the envelope."""
    from fluid_build.copilot.missions.runner import extract_proposed_contract

    wrapped = extract_proposed_contract({"contract": BASE_CONTRACT, "reasoning": "..."})
    assert wrapped["id"] == BASE_CONTRACT["id"]

    bare = extract_proposed_contract(dict(BASE_CONTRACT))
    assert bare["id"] == BASE_CONTRACT["id"]

    # Tolerant about packaging, strict about content.
    assert extract_proposed_contract({"reasoning": "I thought about it"}) is None
    assert extract_proposed_contract({"kind": "SomethingElse"}) is None
    assert extract_proposed_contract(None) is None
    assert extract_proposed_contract({}) is None
