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

"""Multi-turn agent cooperation loop (Missing #10 / E10).

The post-V1.5 audit flagged: *"After Critic flags issues, the
next pass goes through the validator before the critic sees the
retry. World-class agentic systems (DSPy, MetaGPT, AutoGen) loop
until findings are empty."*

This module ships the loop primitive without re-architecting the
coordinator. The loop:

1. Run the agent (pass N).
2. Run the critic against the agent's output.
3. If critic finds error-severity issues AND we're under the cap,
   write feedback to the scratchpad and re-run the agent.
4. Repeat until either critic is clean OR cap hit.

The loop is **opt-in** — agents that want cooperation wrap their
own runs in :func:`run_with_critic_loop`. Existing single-pass
agents are unaffected. Adding cooperation to a stage is a
~5-line wrapper change, not a coordinator refactor.

Design notes:

* **Capped attempts.** Default 3 — same as the staged retry
  envelope. Prevents runaway loops on a stubborn LLM.
* **Empty-progress detection.** If two consecutive passes produce
  the same set of findings (by message hash), we stop early —
  re-running with no new signal won't change the answer.
* **Cost-aware.** Each loop iteration is a full LLM call. The
  cost ceiling check fires automatically (BaseStageAgent already
  checks after every call) so a runaway loop hits the budget
  cap before damage scales.
* **Fully observable.** Every loop iteration emits an event on
  the bus (``cooperation.attempt``). Telemetry exporters /
  audit subscribers see the loop without the agents needing to
  thread anything through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, TypeVar

from fluid_build.copilot.events import Event, get_event_bus
from fluid_build.copilot.scratchpad import (
    CriticFinding,
    Scratchpad,
    StageFeedback,
)

_log = logging.getLogger(__name__)

OutputT = TypeVar("OutputT")


@dataclass
class CooperationOutcome:
    """Result of a critic-cooperation loop.

    ``output`` is the FINAL pass's output (the one closest to
    clean). ``attempts`` is the number of LLM passes made. ``passes``
    is True iff the final pass had zero error-severity critic
    findings — i.e. the loop converged.

    ``finding_history`` records the error-severity findings each
    pass produced so an audit reader can see which signals were
    fixed across iterations and which persisted.
    """

    output: Any
    attempts: int
    passes: bool
    finding_history: List[List[CriticFinding]]


def run_with_critic_loop(
    *,
    stage: str,
    agent_callable: Callable[[Optional[StageFeedback]], OutputT],
    critic_callable: Callable[[OutputT], List[CriticFinding]],
    scratchpad: Scratchpad,
    max_attempts: int = 3,
) -> CooperationOutcome:
    """Run ``agent_callable`` then ``critic_callable`` in a loop
    until the critic is clean or ``max_attempts`` is hit.

    Parameters
    ----------
    stage:
        Stage name for telemetry (e.g. ``"logical"``, ``"builder"``).
        Recorded on every emitted event.
    agent_callable:
        Function that produces the stage's output. Called with
        ``None`` on the first attempt and a :class:`StageFeedback`
        on subsequent attempts (built from the previous critic
        findings).
    critic_callable:
        Function that takes the agent's output and returns a list
        of :class:`CriticFinding` (any severity). The loop exits
        when the returned list has zero ``severity="error"``
        entries.
    scratchpad:
        Scratchpad to write each pass's findings + feedback to.
        Subsequent agent invocations read from it.
    max_attempts:
        Cap on iterations. Default 3.

    Returns
    -------
    :class:`CooperationOutcome`
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be ≥ 1")

    bus = get_event_bus()
    finding_history: List[List[CriticFinding]] = []
    last_signature: Optional[str] = None
    output: Any = None
    attempts = 0
    passes = False

    feedback_for_next: Optional[StageFeedback] = None

    while attempts < max_attempts:
        attempts += 1
        # 1. Run the agent — first attempt has no feedback; later
        # attempts pass the previous loop's StageFeedback so the
        # agent sees what the critic objected to.
        output = agent_callable(feedback_for_next)
        # 2. Run the critic.
        findings = critic_callable(output)
        finding_history.append(list(findings))
        # 3. Record findings on the scratchpad so other agents see
        # them and so the audit trail captures the loop.
        for f in findings:
            scratchpad.add_critic_finding(f)
        errors = [f for f in findings if f.severity == "error"]
        # Telemetry: fire one event per loop iteration.
        bus.emit(
            Event(
                event_type="cooperation.attempt",
                payload={
                    "stage": stage,
                    "attempt": attempts,
                    "max_attempts": max_attempts,
                    "error_count": len(errors),
                    "warning_count": sum(1 for f in findings if f.severity == "warning"),
                    "info_count": sum(1 for f in findings if f.severity == "info"),
                },
            )
        )
        # 4. Done — converged.
        if not errors:
            passes = True
            break
        # 5. Empty-progress detection. If the SAME error signatures
        # show up twice in a row, the agent isn't responding to the
        # feedback; stop early to save tokens.
        signature = "\n".join(sorted(f.message for f in errors))
        if signature == last_signature:
            _log.info(
                "fluid.copilot.cooperation.empty_progress: stage=%s "
                "attempt=%d — same error set as previous pass; stopping",
                stage,
                attempts,
            )
            break
        last_signature = signature
        # 6. Build StageFeedback from the error set for the next
        # agent call. Stored on the scratchpad too so other agents
        # can see the structured feedback.
        feedback = StageFeedback(
            source_stage="critic",
            target_stage=stage,
            summary=(
                f"Critic found {len(errors)} error(s) in stage {stage!r} "
                f"(attempt {attempts}/{max_attempts}). Address these on "
                "the next attempt."
            ),
            structured={
                "errors": [
                    {
                        "message": f.message,
                        "target": f.target,
                        "suggestion": f.suggestion,
                    }
                    for f in errors
                ],
            },
        )
        scratchpad.add_feedback(feedback)
        feedback_for_next = feedback

    # If we exited via the loop condition (not via break), flag the
    # final pass against ``passes``. ``passes`` is True only when
    # the critic returned zero errors on the FINAL attempt.
    return CooperationOutcome(
        output=output,
        attempts=attempts,
        passes=passes,
        finding_history=finding_history,
    )


__all__ = ["CooperationOutcome", "run_with_critic_loop"]
