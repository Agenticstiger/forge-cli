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

"""Plan-then-execute primitives (E13).

World-class agentic systems (ReAct, Reflexion) emit a plan
*before* spending tokens on the answer. The plan lets a critic
review the *intent* before the modeler commits — catching
"build N+1 hubs from this intent" mistakes without paying the
output-token cost of the wrong-shaped answer.

This module ships the typed primitive without re-architecting
the modeler. Agents that want plan-then-execute use:

1. :class:`StagePlan` — typed plan output (modeler emits before
   the actual draft).
2. :func:`record_plan` — adds the plan to the session scratchpad
   so the critic can review it.
3. The critic's existing ``review_*`` rules can be extended to
   read the plan in addition to the output.

The primitive is **opt-in**: agents that don't want plans skip
the call. Adding plans to a stage is a 5-line wrapper change,
not a coordinator refactor.

In v1.6+, the LLM modeler will emit a structured ``StagePlan``
as a separate Pydantic call BEFORE the full output. v1.5 ships
the primitive so callers can adopt incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from fluid_build.copilot.scratchpad import Scratchpad


@dataclass
class PlanStep:
    """One step in a stage plan.

    ``kind`` describes what kind of work this step represents
    (``"create_hub"``, ``"create_link"``, ``"add_satellite"``,
    ``"add_fact"``, etc.). ``target`` is the entity / artifact the
    step produces (``"hub_customer"``, ``"fact_orders"``).
    ``rationale`` is the agent's one-line "why" so the critic
    can sanity-check the intent.
    """

    kind: str
    target: str
    rationale: str = ""
    inputs: List[str] = field(default_factory=list)
    """Source tables / intent sections / prior models the step
    consumes. Lets the critic verify the agent isn't synthesizing
    out of thin air."""


@dataclass
class StagePlan:
    """Full plan one stage emits before executing.

    Plans are intentionally lightweight — a list of typed steps,
    no SQL, no Pydantic blob. They exist purely so the critic /
    operator can object before tokens are spent on the wrong
    output.

    ``stage`` is the stage name (``"logical"``, ``"builder"``).
    ``summary`` is a one-paragraph human summary. ``steps`` is the
    structured action list.
    """

    stage: str
    summary: str
    steps: List[PlanStep] = field(default_factory=list)
    inputs_used: List[str] = field(default_factory=list)
    """Top-level inputs the plan reads (intent paths, table FQNs,
    prior model keys). Should be a superset of every PlanStep's
    ``inputs``."""

    def step_count(self) -> int:
        return len(self.steps)

    def steps_by_kind(self, kind: str) -> List[PlanStep]:
        return [s for s in self.steps if s.kind == kind]


def record_plan(plan: StagePlan, *, scratchpad: Scratchpad) -> None:
    """Write a plan to the scratchpad so other agents see it.

    The plan lands on ``scratchpad.raw["plan:{stage}"]`` so the
    same scratchpad slot can hold plans from multiple stages
    without conflict. Critic agents read via :func:`get_plan`.
    """
    scratchpad.set_raw(f"plan:{plan.stage}", plan)


def get_plan(stage: str, *, scratchpad: Scratchpad) -> Optional[StagePlan]:
    """Read the most recent plan for ``stage`` from the scratchpad."""
    candidate = scratchpad.get_raw(f"plan:{stage}")
    if isinstance(candidate, StagePlan):
        return candidate
    return None


__all__ = [
    "PlanStep",
    "StagePlan",
    "record_plan",
    "get_plan",
]
