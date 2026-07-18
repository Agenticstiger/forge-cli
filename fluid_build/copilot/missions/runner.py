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

"""``MissionRunner`` — the VERIFY-anchored outer loop (deep-agents PR 2).

    VERIFY → PLAN → EXECUTE → GATE → PROGRESS

The load-bearing inversion (RFC-deep-agents.md): **only the code-owned
checks may declare success.** Every cycle starts by re-reading and
re-hashing the on-disk contract and re-running
:func:`~fluid_build.copilot.missions.checks.run_mission_checks` against
it. The LLM plans and edits; it has no channel through which to end the
mission. Three consequences worth stating plainly:

- **Resume is free.** Because VERIFY is idempotent and reads only from
  disk, a paused, stalled, crashed, or stale run re-enters at VERIFY
  with zero replay machinery — the scorecard is simultaneously the
  termination authority and the resume pointer.
- **Self-healing is free.** Failing-check diagnostics (already redacted
  at the checks harness's chokepoint) are recycled verbatim as the next
  cycle's repair feedback, the same shape as
  ``forge_copilot_corrective_feedback.build_corrective_messages``.
  Verification failure *is* the repair prompt.
- **Anti-gaming is enforced, not hoped for.** Every proposed write goes
  through the fail-closed destructive gate before it lands, so a model
  cannot satisfy "every column has a description" by deleting columns.

**Borrow receipts** (searched before building; nothing importable).
*Termination inversion*: smolagents' ``final_answer_checks`` is the
closest public API but it aborts on failure and the model still
initiates via ``final_answer()``; OpenHands' ``CriticBase`` is the
better shape (code-owned ``should_refine()`` vetoing the model's
``FinishAction``, with ``get_followup_prompt()`` feeding failures
forward) but still requires the model to propose finishing. We go
stricter than both — there is no finish action, so the model has no
vote at all. The contrast worth remembering is SWE-agent, whose
termination is a **sentinel substring match** in model output; the
oracle worth emulating is SWE-bench's FAIL_TO_PASS/PASS_TO_PASS, where
the model's opinion is never consulted. *Resume*: this is Kubernetes'
**level-triggered reconcile** — "if your controller crashes and
restarts it doesn't replay missed events, it reads current state and
reconciles". Temporal/LangGraph checkpointing is edge-triggered replay,
and replaying LLM calls raises non-determinism errors by construction;
rejecting event-sourcing here is the same call every K8s controller
makes. Every candidate dependency (guardrails-ai, dspy, instructor,
OpenHands SDK, LangGraph, Temporal) would trip the ``FORBIDDEN_ON_HELP``
set in ``tests/perf/test_startup_budget.py``.

Budgets are hard and cumulative: ``max_usd`` is plumbed into
``RunCostTracker``'s existing per-product ceiling under the scope
``mission:<name>`` (so it applies at every cost-tracked call, not just
at cycle boundaries) *and* re-summed from on-disk receipts each cycle so
pause/resume cannot reset spend. ``max_wall_seconds`` becomes a deadline
checked before every step and every check, with the remaining time
passed down as the per-call LLM timeout. ``max_iterations`` caps cycles.
Overshoot is bounded but nonzero — one in-flight call can cross the
line; that is stated, not hidden.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from fluid_build.copilot.missions.checks import (
    MissionCheckError,
    MissionScorecard,
    load_contract_for_checks,
    run_mission_checks,
)
from fluid_build.copilot.missions.destructive import DiffVerdict, classify_contract_diff
from fluid_build.copilot.missions.gate import confirm_fail_closed, reject_destructive
from fluid_build.copilot.missions.planner import MissionStep, plan_steps
from fluid_build.copilot.missions.spec import MissionSpec
from fluid_build.copilot.missions.store import MissionRunStore, find_resumable_run

LOG = logging.getLogger("fluid.copilot.missions.runner")

#: Operator override for ``budgets.max_wall_seconds`` (RFC "Migration").
WALL_CLOCK_ENV = "FLUID_MISSION_TIMEOUT_SECONDS"

#: The per-product cost-ceiling env var ``RunCostTracker`` already reads.
#: The runner sets it to the effective cap for the duration of the run so
#: ``check_cost_ceiling()`` enforces the mission budget at every
#: cost-tracked LLM call — we reuse the existing mechanism rather than
#: teaching ``cost.py`` about missions.
PER_PRODUCT_LIMIT_ENV = "FLUID_COST_LIMIT_USD_PER_PRODUCT"

#: Inner-loop iteration cap for a single EXECUTE step. The inner agent
#: loop keeps its own 12-iteration default for ordinary forge runs; a
#: mission step is narrower (one repair), so we spend fewer rounds on it.
INNER_LOOP_ITERATIONS = 6

#: PROGRESS: this many consecutive cycles without a strict increase in
#: passing non-advisory checks pauses the run as "stalled".
STALL_PATIENCE = 2

#: Minimum per-call timeout we will pass down. Below this a call is
#: guaranteed to fail, so we stop instead of burning the last seconds.
MIN_CALL_TIMEOUT_SECONDS = 5


def extract_proposed_contract(payload: Any) -> Optional[Dict[str, Any]]:
    """Pull the proposed contract out of an agent-loop payload.

    The loop's documented envelope is ``{"contract": {...}, ...}``, but a
    step-scoped edit prompt reliably provokes the *bare* contract instead
    — the model was handed one document and asked to return it modified,
    so wrapping it feels redundant. Observed live: two of three cycles
    returned a bare ``DataProduct``, and a strict ``payload["contract"]``
    read silently discarded both, which reads as "the model did nothing"
    when in fact it did the work.

    Accept either shape; anything that is not a ``DataProduct`` document
    is rejected, so this is tolerant about packaging without becoming
    tolerant about content.
    """
    if not isinstance(payload, dict) or not payload:
        return None
    candidate = payload.get("contract")
    if isinstance(candidate, dict) and candidate:
        return candidate
    if str(payload.get("kind") or "") == "DataProduct":
        return payload
    return None


class MissionAborted(RuntimeError):
    """Raised internally to unwind to the terminal handler with a reason."""

    def __init__(self, message: str, *, status: str, pause_reason: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.pause_reason = pause_reason


@dataclass
class MissionOutcome:
    """Terminal state of a mission run."""

    status: str  # "complete" | "paused" | "failed"
    run_id: str
    run_dir: Path
    mission: str
    cycles: int = 0
    pause_reason: Optional[str] = None
    scorecard: Optional[MissionScorecard] = None
    spend_usd: float = 0.0
    detail: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "complete"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "mission": self.mission,
            "cycles": self.cycles,
            "pause_reason": self.pause_reason,
            "spend_usd": self.spend_usd,
            "detail": self.detail,
            "scorecard": self.scorecard.to_dict() if self.scorecard else None,
            "events": list(self.events),
        }


class MissionRunner:
    """Runs one mission to a terminal state.

    Every collaborator that touches the outside world is injectable so
    the loop can be tested without an LLM, a TTY, or a clock:
    ``plan_fn`` (PLAN), ``execute_fn`` (EXECUTE), ``confirm_fn`` (GATE),
    ``now_fn`` (deadlines). Production defaults wire the real ones.
    """

    def __init__(
        self,
        spec: MissionSpec,
        contract_path: Path,
        *,
        llm_config: Any = None,
        workspace_root: Optional[Path] = None,
        run_id: Optional[str] = None,
        console: Any = None,
        plan_fn: Optional[Callable[..., Sequence[MissionStep]]] = None,
        execute_fn: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
        confirm_fn: Optional[Callable[..., bool]] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.spec = spec
        self.contract_path = Path(contract_path).resolve()
        self.llm_config = llm_config
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.console = console
        self._plan_fn = plan_fn or plan_steps
        self._execute_fn = execute_fn or self._execute_with_agent_loop
        self._confirm_fn = confirm_fn or confirm_fail_closed
        self._now = now_fn or time.monotonic
        self.store = MissionRunStore(self.workspace_root, run_id or self._new_run_id())
        self.events: List[Dict[str, Any]] = []
        self._prior_spend_usd = 0.0
        self._deadline: Optional[float] = None
        #: The contract as it stood when this run started — the
        #: destructive gate's reference point. See :meth:`_gate`.
        self._baseline_contract: Optional[Dict[str, Any]] = None

    # ── construction helpers ───────────────────────────────────────

    @staticmethod
    def _new_run_id() -> str:
        from fluid_build.cli._preview_panel import new_run_id

        return new_run_id()

    @classmethod
    def resume(
        cls,
        spec: MissionSpec,
        contract_path: Path,
        *,
        workspace_root: Optional[Path] = None,
        run_id: Optional[str] = None,
        **kwargs: Any,
    ) -> "MissionRunner":
        """Re-open the newest resumable run for *spec*, or start a new one.

        There is no replay: resuming simply constructs the runner over
        the existing run directory. The next thing that happens is
        VERIFY against the current on-disk contract, which is exactly
        what a fresh run does.
        """
        root = (workspace_root or Path.cwd()).resolve()
        manifest = find_resumable_run(root, mission=spec.name, run_id=run_id)
        resolved_run_id = str(manifest.get("run_id")) if manifest else run_id
        runner = cls(
            spec,
            contract_path,
            workspace_root=root,
            run_id=resolved_run_id,
            **kwargs,
        )
        runner._resumed_from = manifest  # type: ignore[attr-defined]
        return runner

    # ── budgets ────────────────────────────────────────────────────

    def _effective_usd_cap(self) -> Optional[float]:
        """min(spec budget, operator env) — the tighter cap always wins."""
        caps: List[float] = []
        spec_cap = self.spec.budgets.max_usd
        if spec_cap is not None and spec_cap > 0:
            caps.append(float(spec_cap))
        env_cap = os.environ.get(PER_PRODUCT_LIMIT_ENV)
        if env_cap:
            try:
                value = float(env_cap)
                if value > 0:
                    caps.append(value)
            except (TypeError, ValueError):
                pass
        return min(caps) if caps else None

    def _wall_seconds(self) -> Optional[int]:
        override = os.environ.get(WALL_CLOCK_ENV)
        if override:
            try:
                value = int(float(override))
                if value > 0:
                    return value
            except (TypeError, ValueError):
                LOG.warning("mission_bad_timeout_env", extra={"value": override})
        budget = self.spec.budgets.max_wall_seconds
        return int(budget) if budget else None

    def _spend_usd(self) -> float:
        """Cumulative spend: on-disk receipts + this process's tracker.

        The receipts are the authority across pause/resume — a fresh
        process starts with an empty tracker, so trusting it alone would
        silently reset a mission's budget every time it resumed.
        """
        live = 0.0
        try:
            from fluid_build.copilot.cost import get_run_tracker

            total = get_run_tracker().breakdown().total_usd
            if isinstance(total, (int, float)):
                live = float(total)
        except Exception:  # noqa: BLE001 — budget accounting must not crash the run
            LOG.debug("mission_live_spend_unavailable", exc_info=True)
        return round(self._prior_spend_usd + live, 6)

    def _remaining_seconds(self) -> Optional[float]:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - self._now())

    def _check_budgets(self) -> None:
        """Raise :class:`MissionAborted` when a hard ceiling is reached."""
        cap = self._effective_usd_cap()
        if cap is not None:
            spend = self._spend_usd()
            if spend >= cap:
                raise MissionAborted(
                    f"budget ceiling reached: ${spend:.4f} of ${cap:.2f}",
                    status="paused",
                    pause_reason="budget",
                )
        remaining = self._remaining_seconds()
        if remaining is not None and remaining <= MIN_CALL_TIMEOUT_SECONDS:
            raise MissionAborted(
                f"wall-clock deadline reached ({self._wall_seconds()}s)",
                status="paused",
                pause_reason="timeout",
            )

    def _call_llm_config(self) -> Any:
        """The mission's LLM config with the deadline as the call timeout.

        Same posture as ``FLUID_TOFU_TIMEOUT_SECONDS``: a hung call can
        overshoot the mission deadline by at most one call's timeout.
        """
        if self.llm_config is None:
            return None
        remaining = self._remaining_seconds()
        if remaining is None:
            return self.llm_config
        base = getattr(self.llm_config, "timeout_seconds", None) or int(remaining)
        capped = max(MIN_CALL_TIMEOUT_SECONDS, int(min(float(base), remaining)))
        try:
            return dataclasses.replace(self.llm_config, timeout_seconds=capped)
        except Exception:  # noqa: BLE001 — non-dataclass config (tests)
            return self.llm_config

    # ── console / events ───────────────────────────────────────────

    def _emit(self, message: str) -> None:
        if self.console is None:
            return
        try:
            self.console.print(message)
        except Exception:  # noqa: BLE001 — never crash the loop on rendering
            pass

    def _event(self, kind: str, **fields: Any) -> None:
        payload = {"event": kind, **fields}
        self.events.append(payload)
        LOG.info(kind, extra=fields)

    # ── VERIFY ─────────────────────────────────────────────────────

    def verify(self) -> MissionScorecard:
        """Re-read, re-hash, re-run every check. The only success oracle."""
        scorecard = run_mission_checks(self.spec, self.contract_path)
        self._event(
            "mission_verify",
            mission=self.spec.name,
            passed=scorecard.passed,
            gating_passed=scorecard.gating_passed,
            gating_total=scorecard.gating_total,
            contract_sha256=scorecard.contract_sha256,
        )
        return scorecard

    # ── EXECUTE ────────────────────────────────────────────────────

    def _execute_deterministic(
        self, step: MissionStep, contract: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Run the mapped deterministic helper on a scratch copy.

        Hardcoded routing for the two v1 mappings (RFC open question #3
        — two entries do not justify a registry). No LLM, no cost, and
        the result still goes through the destructive gate like any
        other proposed write.
        """
        import copy

        scratch = copy.deepcopy(contract)
        if step.action == "enforce_ai_ready":
            from fluid_build.copilot.agents.ai_ready_agent import enforce_ai_ready

            enforce_ai_ready(scratch)
            return scratch
        if step.action == "enrich_contract":
            from fluid_build.cli.forge_copilot_runtime import _enrich_contract

            enriched = _enrich_contract(scratch)
            return enriched if isinstance(enriched, dict) else scratch
        return None  # pragma: no cover — guarded by MissionStep.deterministic

    def _execute_with_agent_loop(
        self,
        step: MissionStep,
        contract: Dict[str, Any],
        *,
        spec: MissionSpec,
        llm_config: Any,
        workspace_root: Path,
        console: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """Run one repair step through the bounded inner agent loop.

        The two mission-specific parameters are the honest surgery the
        RFC called for: ``tool_allowlist`` (``spec.tools.allow``,
        intersected with the live registry inside the loop) and
        ``goal_scope`` (this step's framing). The seed — the contract as
        it exists on disk right now — rides the existing
        ``seed_contract_override`` seam via ``forge_copilot_runtime``.
        """
        from fluid_build.cli.forge_copilot_agent_loop import run_copilot_agent_loop
        from fluid_build.cli.forge_copilot_runtime import build_agent_loop_seed_context

        context = build_agent_loop_seed_context(
            {"project_goal": spec.goal},
            seed_contract=contract,
        )
        payload = run_copilot_agent_loop(
            context=context,
            llm_config=llm_config,
            workspace_root=workspace_root,
            console=console,
            max_iterations=INNER_LOOP_ITERATIONS,
            tool_allowlist=list(spec.tools_allow) or None,
            goal_scope=step.goal,
        )
        return extract_proposed_contract(payload)

    # ── GATE ───────────────────────────────────────────────────────

    def _gate(
        self,
        step: MissionStep,
        old_contract: Dict[str, Any],
        new_contract: Dict[str, Any],
    ) -> Tuple[bool, str, DiffVerdict]:
        """Classify the diff and decide whether the write may land.

        Returns ``(approved, reason, verdict)`` — the verdict rides along
        so the caller can tell the operator which findings blocked the
        write. Unknown diff shapes classify
        destructive (see :mod:`destructive`), ``gates.destructive: deny``
        refuses outright, and the interactive path is
        :func:`confirm_fail_closed` — which rejects on a non-TTY. There
        is no ``--yes`` channel into this function by construction.

        **The diff is anchored to the contract as it was at mission
        start, not to the previous step's output.** A live run made the
        reason obvious: the model added a schema-invalid ``dq.rules``
        entry in cycle 1, then spent cycles 2-3 trying to correct it —
        and a step-anchored gate rejected every correction, because
        fixing a malformed block means removing the malformed keys. The
        gate was protecting the model's own bad output from the model's
        own repair, and the mission could never converge.

        Baseline anchoring states the actual policy: content that
        existed before the mission started is the operator's and is
        protected; content the mission itself authored is the mission's
        and may be revised freely. Deleting a pre-existing column is
        still destructive on cycle 1 and on cycle 6 alike.
        """
        baseline = self._baseline_contract if self._baseline_contract is not None else old_contract
        verdict = classify_contract_diff(baseline, new_contract)
        if not verdict.changed:
            return (False, "no_change", verdict)
        if not verdict.destructive:
            return (True, "non_destructive", verdict)

        summary = verdict.summary_lines()
        if self.spec.gates.destructive == "deny":
            reject_destructive(summary, mission=self.spec.name, step=step.action)
            self._event(
                "mission_destructive_gate_rejected",
                mission=self.spec.name,
                step=step.action,
                reason="gates_destructive_deny",
                findings=summary,
            )
            return (False, "denied_by_spec", verdict)

        approved = self._confirm_fn(
            summary,
            mission=self.spec.name,
            step=step.action,
        )
        self._event(
            (
                "mission_destructive_gate_approved"
                if approved
                else "mission_destructive_gate_rejected"
            ),
            mission=self.spec.name,
            step=step.action,
            findings=summary,
        )
        return (approved, "approved" if approved else "rejected", verdict)

    # ── contract IO ────────────────────────────────────────────────

    def _write_contract(self, contract: Dict[str, Any]) -> None:
        from fluid_build.cli.forge_contract_factory import write_contract

        write_contract(contract, self.contract_path, command=f"fluid mission run {self.spec.name}")

    def _reread(self) -> Tuple[Dict[str, Any], str]:
        return load_contract_for_checks(self.contract_path)

    # ── the loop ───────────────────────────────────────────────────

    def run(self) -> MissionOutcome:
        """Drive VERIFY → PLAN → EXECUTE → GATE → PROGRESS to a terminus."""
        wall = self._wall_seconds()
        self._deadline = (self._now() + wall) if wall else None
        self._prior_spend_usd = self.store.spend_from_receipts()
        # Capture the gate's baseline before any step can touch the file.
        try:
            self._baseline_contract, _ = self._reread()
        except MissionCheckError:
            # An unreadable contract is VERIFY's problem to report, not
            # the gate's; leave the baseline unset and let the loop fail
            # through the normal path.
            self._baseline_contract = None

        max_iterations = int(self.spec.budgets.max_iterations or 1)
        self.store.update_manifest(
            status="running",
            mission=self.spec.name,
            mission_goal=self.spec.goal,
            mission_spec_sha256=self.spec.content_sha256,
            contract_path=str(self.contract_path),
            pause_reason=None,
        )
        self._event(
            "mission_started",
            mission=self.spec.name,
            run_id=self.store.run_id,
            resumed=bool(getattr(self, "_resumed_from", None)),
            prior_spend_usd=self._prior_spend_usd,
            max_iterations=max_iterations,
        )

        scorecard: Optional[MissionScorecard] = None
        cycle = 0
        progress_history: List[int] = []

        product_scope = f"mission:{self.spec.name}"
        with self._cost_scope(product_scope):
            try:
                for cycle in range(1, max_iterations + 1):
                    # ---- 1. VERIFY (the resume pointer + success oracle)
                    scorecard = self.verify()
                    self.store.write_scorecard(scorecard.to_dict(), cycle=cycle)
                    self._persist_cycle_cost(cycle)
                    self._emit(
                        f"  VERIFY  cycle {cycle}/{max_iterations} — "
                        f"{scorecard.gating_passed}/{scorecard.gating_total} checks passing"
                    )
                    if scorecard.passed:
                        return self._terminate(
                            "complete",
                            scorecard=scorecard,
                            cycle=cycle,
                            detail="all non-advisory checks pass",
                        )

                    self._check_budgets()

                    # ---- 2. PLAN
                    steps = self._plan(scorecard, cycle)

                    # ---- 3/4. EXECUTE + GATE
                    self._run_steps(steps, cycle)

                    # ---- 5. PROGRESS
                    progress_history.append(scorecard.gating_passed)
                    if self._stalled(progress_history):
                        # Re-verify first: the final cycle's edits may
                        # have landed after the metric was sampled.
                        scorecard = self.verify()
                        self.store.write_scorecard(scorecard.to_dict(), cycle=cycle)
                        if scorecard.passed:
                            return self._terminate(
                                "complete",
                                scorecard=scorecard,
                                cycle=cycle,
                                detail="all non-advisory checks pass",
                            )
                        raise MissionAborted(
                            f"no improvement for {STALL_PATIENCE} consecutive cycles",
                            status="paused",
                            pause_reason="stalled",
                        )

                # Iteration cap: one final VERIFY so the terminal
                # scorecard reflects the last cycle's edits.
                scorecard = self.verify()
                self.store.write_scorecard(scorecard.to_dict(), cycle=max_iterations)
                if scorecard.passed:
                    return self._terminate(
                        "complete",
                        scorecard=scorecard,
                        cycle=max_iterations,
                        detail="all non-advisory checks pass",
                    )
                return self._terminate(
                    "paused",
                    scorecard=scorecard,
                    cycle=max_iterations,
                    pause_reason="iterations",
                    detail=f"iteration cap reached ({max_iterations})",
                )

            except MissionAborted as abort:
                return self._terminate(
                    abort.status,
                    scorecard=scorecard,
                    cycle=cycle,
                    pause_reason=abort.pause_reason,
                    detail=str(abort),
                )
            except MissionCheckError as exc:
                return self._terminate(
                    "failed",
                    scorecard=scorecard,
                    cycle=cycle,
                    detail=f"contract unreadable: {exc}",
                )
            except KeyboardInterrupt:
                return self._terminate(
                    "paused",
                    scorecard=scorecard,
                    cycle=cycle,
                    pause_reason="stalled",
                    detail="interrupted by operator",
                )

    # ── loop internals ─────────────────────────────────────────────

    def _plan(self, scorecard: MissionScorecard, cycle: int) -> Sequence[MissionStep]:
        steps = self._plan_fn(
            self.spec,
            scorecard,
            llm_config=self._call_llm_config(),
        )
        payload = {"cycle": cycle, "steps": [s.to_dict() for s in steps]}
        self.store.write_plan(payload, cycle=cycle)
        self._event("mission_planned", mission=self.spec.name, cycle=cycle, steps=len(steps))
        self._emit(f"  PLAN    {len(steps)} step(s): " + ", ".join(s.action for s in steps))
        return steps

    def _run_steps(self, steps: Sequence[MissionStep], cycle: int) -> None:
        for index, step in enumerate(steps, start=1):
            self._check_budgets()
            contract, hash_before = self._reread()
            self._emit(f"  EXECUTE step {index}/{len(steps)} [{step.action}] {step.goal[:80]}")

            try:
                if step.deterministic:
                    proposed = self._execute_deterministic(step, contract)
                else:
                    proposed = self._execute_fn(
                        step,
                        contract,
                        spec=self.spec,
                        llm_config=self._call_llm_config(),
                        workspace_root=self.workspace_root,
                        console=self.console,
                    )
            except MissionAborted:
                raise
            except Exception as exc:  # noqa: BLE001 — a failed step is not a failed mission
                # Typed name only: executor exception text never
                # round-trips into the next cycle's LLM context
                # (the PRs #28–#33 invariant).
                LOG.warning(
                    "mission_step_failed",
                    extra={
                        "mission": self.spec.name,
                        "step": step.action,
                        "error": type(exc).__name__,
                    },
                    exc_info=True,
                )
                self._event(
                    "mission_step_failed",
                    mission=self.spec.name,
                    step=step.action,
                    error=type(exc).__name__,
                )
                self._emit(f"          step failed ({type(exc).__name__}) — continuing")
                continue

            if not isinstance(proposed, dict) or not proposed:
                self._event("mission_step_no_contract", mission=self.spec.name, step=step.action)
                continue

            # Concurrency: re-hash immediately before the write. If the
            # contract moved under us, abandon the step and re-enter
            # VERIFY — StaleContractError semantics extended from
            # pause/resume to the per-step read-modify-write window.
            _, hash_now = self._reread()
            if hash_now != hash_before:
                self._event(
                    "mission_contract_changed_out_of_band",
                    mission=self.spec.name,
                    step=step.action,
                )
                self._emit("          contract changed out-of-band — re-verifying")
                return

            approved, reason, verdict = self._gate(step, contract, proposed)
            if not approved:
                self._emit(f"          write blocked ({reason})")
                # Say WHY. A gate that refuses without naming the finding
                # is indistinguishable from a broken runner, and the
                # operator is the one who has to decide if it was right.
                for line in verdict.summary_lines(limit=5):
                    self._emit(f"            · {line}")
                continue

            self._write_contract(proposed)
            self._event("mission_step_applied", mission=self.spec.name, step=step.action)
            self._emit("          edit applied")

    def _stalled(self, history: List[int]) -> bool:
        """No strict increase for :data:`STALL_PATIENCE` consecutive cycles.

        **Do not "simplify" this into repetition matching.** The obvious
        alternative — compare successive actions/outputs for similarity,
        as OpenHands' ``StuckDetector`` does — has a documented
        false-positive class: OpenHands issues #5355 and #7183 are both
        agents killed while legitimately polling a long-running process,
        because repetition can't be told apart from patience. Counting
        *passing checks* is immune to that: it only ever moves when real,
        externally-verified progress happens, so waiting looks like
        waiting and stalling looks like stalling.
        """
        if len(history) <= STALL_PATIENCE:
            return False
        window = history[-(STALL_PATIENCE + 1) :]
        return all(later <= earlier for earlier, later in zip(window, window[1:], strict=False))

    def _persist_cycle_cost(self, cycle: int) -> None:
        """Write this cycle's cost receipt — the cumulative-budget source."""
        try:
            from fluid_build.copilot.cost import get_run_tracker

            get_run_tracker().persist_to_run_dir(
                self.store.cycle_dir(cycle),
                provider=str(getattr(self.llm_config, "provider", "") or ""),
                model=str(getattr(self.llm_config, "model", "") or ""),
            )
        except Exception:  # noqa: BLE001 — a missing receipt must not end the run
            LOG.debug("mission_cost_receipt_failed", exc_info=True)

    def _cost_scope(self, product_scope: str) -> Any:
        """Context manager: per-product cost attribution + hard ceiling.

        Pushes ``mission:<name>`` onto ``RunCostTracker``'s existing
        product stack and sets the per-product ceiling env var to the
        effective cap, so ``check_cost_ceiling()`` — already called after
        every tracked LLM call — enforces the mission's budget in-call
        rather than only at cycle boundaries. Both are restored on exit.
        """
        from contextlib import contextmanager

        @contextmanager
        def _scope():
            from fluid_build.copilot.cost import get_run_tracker

            cap = self._effective_usd_cap()
            previous = os.environ.get(PER_PRODUCT_LIMIT_ENV)
            tracker = get_run_tracker()
            tracker.push_product(product_scope)
            if cap is not None:
                # Charge the already-spent (resumed) total against the
                # cap so a resumed run cannot get a fresh full budget.
                remaining = max(0.0, cap - self._prior_spend_usd)
                os.environ[PER_PRODUCT_LIMIT_ENV] = f"{remaining:.6f}"
            try:
                yield
            finally:
                tracker.pop_product()
                if previous is None:
                    os.environ.pop(PER_PRODUCT_LIMIT_ENV, None)
                else:
                    os.environ[PER_PRODUCT_LIMIT_ENV] = previous

        return _scope()

    def _terminate(
        self,
        status: str,
        *,
        scorecard: Optional[MissionScorecard],
        cycle: int,
        pause_reason: Optional[str] = None,
        detail: str = "",
    ) -> MissionOutcome:
        """Write the terminal manifest and build the outcome.

        ``complete``/``failed`` are written **explicitly** — nothing else
        flips a mission out of ``running``, so a finished mission never
        lingers in the resumable set.
        """
        self._persist_cycle_cost(max(cycle, 1))
        spend = self._spend_usd()
        self.store.update_manifest(
            status=status,
            pause_reason=pause_reason,
            mission=self.spec.name,
            mission_goal=self.spec.goal,
            mission_spec_sha256=self.spec.content_sha256,
            contract_path=str(self.contract_path),
            cycles=cycle,
            total_cost_usd=spend,
            criteria_status=(
                {
                    "passed": scorecard.gating_passed,
                    "total": scorecard.gating_total,
                    "contract_sha256": scorecard.contract_sha256,
                }
                if scorecard
                else None
            ),
        )
        self._event(
            "mission_finished",
            mission=self.spec.name,
            run_id=self.store.run_id,
            status=status,
            pause_reason=pause_reason,
            cycles=cycle,
            spend_usd=spend,
        )
        return MissionOutcome(
            status=status,
            run_id=self.store.run_id,
            run_dir=self.store.run_dir,
            mission=self.spec.name,
            cycles=cycle,
            pause_reason=pause_reason,
            scorecard=scorecard,
            spend_usd=spend,
            detail=detail,
            events=list(self.events),
        )


def run_mission(
    spec: MissionSpec,
    contract_path: Path,
    *,
    llm_config: Any = None,
    workspace_root: Optional[Path] = None,
    resume: bool = False,
    run_id: Optional[str] = None,
    console: Any = None,
    **kwargs: Any,
) -> MissionOutcome:
    """Convenience entry point used by ``fluid mission run``."""
    factory = MissionRunner.resume if resume else MissionRunner
    runner = factory(  # type: ignore[operator]
        spec,
        contract_path,
        llm_config=llm_config,
        workspace_root=workspace_root,
        run_id=run_id,
        console=console,
        **kwargs,
    )
    return runner.run()


__all__ = [
    "INNER_LOOP_ITERATIONS",
    "PER_PRODUCT_LIMIT_ENV",
    "STALL_PATIENCE",
    "WALL_CLOCK_ENV",
    "MissionAborted",
    "MissionOutcome",
    "MissionRunner",
    "run_mission",
]
