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

"""PLAN — the mission loop's one cheap LLM call per cycle.

The planner turns the failing half of a scorecard into an ordered list
of repair steps. Three properties make it safe to let a model do this:

1. **It cannot declare success.** The planner's output is a work list,
   never a verdict; only :func:`~fluid_build.copilot.missions.checks.run_mission_checks`
   terminates a mission.
2. **It cannot widen capability.** ``plan_hint`` is an *ordering* hint
   the planner may reorder or drop, never extend, and the tool
   allowlist it feeds is ``spec.tools.allow`` — resolved by the runner,
   not the model.
3. **It only ever sees redacted text.** The diagnostics handed to it
   have already passed the secret redactor at the checks harness's
   single chokepoint, and this module re-clamps length so a
   pathological contract cannot flood the prompt.

Failure is not fatal: an unparseable / errored plan degrades to
:func:`fallback_plan`, which recycles the failing-check diagnostics
verbatim as one repair step. That mirrors the existing self-healing
shape in ``cli/forge_copilot_corrective_feedback.py`` — verification
failure *is* the repair prompt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

LOG = logging.getLogger("fluid.copilot.missions.planner")

#: Deterministic helpers the runner can run instead of an LLM step.
#: Hardcoded, not a registry — two entries do not justify one, and the
#: RFC's open question #3 says extract one only when v2's check plugins
#: force the question.
DETERMINISTIC_ACTIONS = ("enforce_ai_ready", "enrich_contract")

#: The generic LLM-driven action. Anything the planner emits that isn't
#: a deterministic helper collapses to this — the planner picks *what to
#: work on*, never *what machinery runs*.
EDIT_ACTION = "edit_contract"

MAX_STEPS = 6
MAX_GOAL_CHARS = 400
MAX_DIAGNOSTIC_LINES = 20


@dataclass(frozen=True)
class MissionStep:
    """One planned unit of work."""

    action: str
    goal: str

    @property
    def deterministic(self) -> bool:
        return self.action in DETERMINISTIC_ACTIONS

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "goal": self.goal, "deterministic": self.deterministic}


def failing_diagnostics(scorecard: Any) -> List[str]:
    """Redacted, length-clamped diagnostics from every failing check.

    These lines are the mission's repair feedback: they come straight
    out of the code-owned checks (already redacted by the harness), so
    what the planner reads is exactly what failed — no model-authored
    restatement in between.
    """
    lines: List[str] = []
    for result in getattr(scorecard, "results", []) or []:
        if getattr(result, "passed", False) or getattr(result, "advisory", False):
            continue
        name = getattr(result, "name", "check")
        detail = getattr(result, "detail", "") or ""
        lines.append(f"[{name}] {detail}")
        for diag in (getattr(result, "diagnostics", None) or [])[:MAX_DIAGNOSTIC_LINES]:
            lines.append(f"    - {diag}")
    return lines[: MAX_DIAGNOSTIC_LINES * 2]


def build_plan_prompt(spec: Any, scorecard: Any) -> str:
    """The planner's user prompt — goal + what is currently failing."""
    diagnostics = failing_diagnostics(scorecard)
    hints = list(getattr(spec, "plan_hint", ()) or ())
    tools = list(getattr(spec, "tools_allow", ()) or ())
    goal = " ".join(str(getattr(spec, "goal", "")).split())
    return (
        f"MISSION GOAL:\n{goal}\n\n"
        f"FAILING SUCCESS CRITERIA (produced by deterministic code-owned checks "
        f"run against the on-disk contract — this is ground truth, not opinion):\n"
        + ("\n".join(diagnostics) if diagnostics else "(none reported)")
        + "\n\n"
        + (f"SUGGESTED STEP ORDER (hint only): {', '.join(hints)}\n" if hints else "")
        + (f"TOOLS AVAILABLE TO THE EXECUTOR: {', '.join(tools)}\n" if tools else "")
        + "\nProduce the shortest ordered list of steps that would make the "
        "failing criteria pass. Return STRICT JSON only:\n"
        '{"steps": [{"action": "<one of: '
        + ", ".join([*DETERMINISTIC_ACTIONS, EDIT_ACTION])
        + '>", "goal": "<one sentence, imperative>"}]}\n'
        f"At most {MAX_STEPS} steps. Do not add tools. Do not claim the mission "
        "is complete — you cannot; only the checks decide that."
    )


PLAN_SYSTEM_PROMPT = (
    "You are the planner for a FLUID data-product mission. You plan "
    "repairs to a data-product contract; you never decide whether the "
    "mission succeeded — deterministic code-owned checks do. Return "
    "strict JSON only, with no prose and no code fences."
)


def _coerce_steps(payload: Any) -> List[MissionStep]:
    """Coerce a parsed planner payload into validated steps.

    Unknown actions collapse to :data:`EDIT_ACTION` rather than being
    rejected — the planner choosing a label we don't know must not stall
    the mission, and the executor's capability is bounded by the tool
    allowlist regardless of what the step is called.
    """
    if not isinstance(payload, dict):
        return []
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return []
    steps: List[MissionStep] = []
    for raw in raw_steps[:MAX_STEPS]:
        if not isinstance(raw, dict):
            continue
        goal = str(raw.get("goal") or "").strip()[:MAX_GOAL_CHARS]
        if not goal:
            continue
        action = str(raw.get("action") or "").strip()
        if action not in DETERMINISTIC_ACTIONS:
            action = EDIT_ACTION
        steps.append(MissionStep(action=action, goal=goal))
    return steps


def fallback_plan(scorecard: Any) -> List[MissionStep]:
    """Deterministic plan used when the planner call fails.

    One ``edit_contract`` step whose goal *is* the failing diagnostics.
    A mission that cannot reach its planner still makes progress; the
    checks remain the only thing that can end it.
    """
    diagnostics = failing_diagnostics(scorecard)
    joined = " ".join(" ".join(diagnostics).split())[:MAX_GOAL_CHARS]
    goal = (
        f"Fix the failing success criteria: {joined}"
        if joined
        else "Fix the failing success criteria reported by the mission checks."
    )
    return [MissionStep(action=EDIT_ACTION, goal=goal)]


def plan_steps(
    spec: Any,
    scorecard: Any,
    *,
    llm_config: Any,
    call_llm_fn: Optional[Any] = None,
    provider: Optional[Any] = None,
) -> Sequence[MissionStep]:
    """Ask the LLM for an ordered repair plan; degrade gracefully.

    ``call_llm_fn`` / ``provider`` are the test seams. In production both
    resolve to :mod:`fluid_build.llm.providers` — the cost-tracked call
    path, so planner spend lands in ``RunCostTracker`` and is subject to
    the mission's per-product ceiling like every other call.
    """
    from fluid_build.cli.forge_copilot_runtime import extract_json_object

    if call_llm_fn is None or provider is None:
        from fluid_build.llm.providers import call_llm as _call_llm
        from fluid_build.llm.providers import get_llm_provider

        call_llm_fn = call_llm_fn or _call_llm
        provider = provider or get_llm_provider(llm_config.provider)

    prompt = build_plan_prompt(spec, scorecard)
    try:
        raw = call_llm_fn(provider, llm_config, PLAN_SYSTEM_PROMPT, prompt)
    except Exception as exc:  # noqa: BLE001 — planner failure is recoverable
        LOG.warning(
            "mission_plan_call_failed",
            extra={"mission": getattr(spec, "name", ""), "error": type(exc).__name__},
        )
        return fallback_plan(scorecard)

    try:
        payload = extract_json_object(raw or "")
    except (ValueError, json.JSONDecodeError):
        LOG.info(
            "mission_plan_unparseable",
            extra={"mission": getattr(spec, "name", "")},
        )
        return fallback_plan(scorecard)

    steps = _coerce_steps(payload)
    if not steps:
        return fallback_plan(scorecard)
    return steps


__all__ = [
    "DETERMINISTIC_ACTIONS",
    "EDIT_ACTION",
    "MAX_STEPS",
    "MissionStep",
    "PLAN_SYSTEM_PROMPT",
    "build_plan_prompt",
    "failing_diagnostics",
    "fallback_plan",
    "plan_steps",
]
