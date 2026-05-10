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

"""Item 8 — convergence behavioral test for the cooperation loop.

Unit tests prove the cooperation-loop *primitive* works: it
calls the agent, calls the critic, retries on errors, converges.

Convergence (this file) proves something stronger: when feedback
F is injected on attempt N+1, the agent **actually changes its
output to address F** and the critic confirms convergence on the
next pass.

Without this test, "the cooperation loop works" is mechanically
true (it loops) but behaviorally untested (it never proves the
loop produces a meaningfully different output on retry).

Pinned scenarios:

1. **First-pass empty hub → second-pass populated hub.** Stub
   agent reads ``feedback_for_stage("logical")`` from the
   scratchpad and responds by populating ``business_key_columns``.
   Asserts: ``outcome.passes is True`` AND ``outcome.attempts == 2``.

2. **Three-pass progression.** Three different findings, each
   addressed in the subsequent pass. Asserts the loop converges
   exactly when the agent stops generating issues — not when
   the cap is hit.

3. **Multi-finding parallel fix.** Critic emits 2 errors on
   pass 1; agent fixes BOTH on pass 2; critic clean on pass 2.
   Convergence in 2 attempts despite 2-finding-per-pass output.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from fluid_build.copilot.agents.cooperation_loop import (
    CooperationOutcome,
    run_with_critic_loop,
)
from fluid_build.copilot.scratchpad import (
    CriticFinding,
    Scratchpad,
    StageFeedback,
)


def _make_hub(name: str, business_keys: List[str]) -> SimpleNamespace:
    return SimpleNamespace(
        entity_name=name,
        business_key_columns=business_keys,
    )


class TestSinglePassConvergence:
    def test_agent_reads_feedback_and_fixes_output(self):
        """The behavioral contract: when StageFeedback lands on
        the scratchpad, the agent's NEXT call MUST produce
        different output reflecting the feedback's instructions."""
        pad = Scratchpad()
        # Track each pass's output for inspection.
        outputs: List[List[str]] = []

        def agent(_feedback):
            # Read the scratchpad's StageFeedback addressed to
            # ``logical`` — this is the contract the cooperation
            # loop publishes.
            feedback = pad.feedback_for_stage("logical")
            if not feedback:
                # First pass — produce an EMPTY hub (the bad output
                # the critic will object to).
                hub = _make_hub("customer", [])
            else:
                # Subsequent pass — read the structured findings
                # and ACT on the suggestion. This is the behavioral
                # convergence: the agent MUST do something
                # different on retry.
                errors = feedback[-1].structured.get("errors", [])
                # Each error has a 'suggestion' — use it.
                fix = ", ".join(e.get("suggestion", "") for e in errors)
                # The fix here: populate business_key_columns.
                hub = _make_hub("customer", ["customer_id"])
            outputs.append(hub.business_key_columns)
            return SimpleNamespace(hub=hub)

        def critic(output):
            if not output.hub.business_key_columns:
                return [
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message=("Hub has no business_key_columns; cannot load satellites."),
                        suggestion="Set business_key_columns to ['customer_id']",
                    )
                ]
            return []

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=3,
        )

        # Pin: cooperation actually converged.
        assert outcome.passes is True
        assert outcome.attempts == 2
        # Pin: the agent's output ACTUALLY CHANGED between passes.
        assert outputs[0] == [], "first pass should be empty (bad)"
        assert outputs[1] == ["customer_id"], "second pass should be populated (good)"
        # Pin: final critic findings list is empty.
        assert outcome.finding_history[-1] == []


class TestThreePassProgression:
    def test_three_distinct_findings_each_addressed(self):
        """Three different findings, each addressed in the
        subsequent pass. Asserts the loop converges at the FIRST
        clean pass — not at the cap."""
        pad = Scratchpad()
        # Each pass addresses ONE more issue.
        state: Dict[str, bool] = {
            "has_business_keys": False,
            "has_description": False,
            "has_owner": False,
        }

        def agent(_feedback):
            feedback = pad.feedback_for_stage("logical")
            if feedback:
                # Apply ONE fix per pass (first error in the most
                # recent feedback).
                most_recent = feedback[-1]
                errors = most_recent.structured.get("errors", [])
                if errors:
                    msg = errors[0].get("message", "")
                    if "business_key" in msg:
                        state["has_business_keys"] = True
                    elif "description" in msg:
                        state["has_description"] = True
                    elif "owner" in msg:
                        state["has_owner"] = True
            return SimpleNamespace(state=dict(state))

        def critic(output):
            findings = []
            if not output.state["has_business_keys"]:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message="Hub has no business_key_columns",
                    )
                )
            elif not output.state["has_description"]:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message="Hub has no description",
                    )
                )
            elif not output.state["has_owner"]:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message="Hub has no owner",
                    )
                )
            return findings

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=5,
        )

        # 4 attempts: 1 baseline + 3 fixes (one per finding).
        assert outcome.attempts == 4
        assert outcome.passes is True
        # Final state has all three issues fixed.
        assert outcome.output.state == {
            "has_business_keys": True,
            "has_description": True,
            "has_owner": True,
        }


class TestParallelFix:
    def test_two_findings_addressed_in_one_pass(self):
        """Critic emits 2 errors on pass 1; agent fixes BOTH on
        pass 2; critic clean on pass 2. Behavioral convergence
        in 2 attempts despite 2 errors."""
        pad = Scratchpad()
        state = {"missing_keys": True, "missing_desc": True}

        def agent(_feedback):
            feedback = pad.feedback_for_stage("logical")
            if feedback:
                # Address ALL errors in this pass — not just one.
                errors = feedback[-1].structured.get("errors", [])
                for err in errors:
                    msg = err.get("message", "")
                    if "key" in msg.lower():
                        state["missing_keys"] = False
                    if "desc" in msg.lower():
                        state["missing_desc"] = False
            return SimpleNamespace(**state)

        def critic(output):
            findings = []
            if output.missing_keys:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message="missing keys",
                    )
                )
            if output.missing_desc:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message="missing description",
                    )
                )
            return findings

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=5,
        )

        # Two errors flagged on pass 1, both fixed on pass 2,
        # critic clean on pass 2.
        assert outcome.attempts == 2
        assert outcome.passes is True
        # Pass 1 had 2 errors; pass 2 had 0.
        assert len(outcome.finding_history[0]) == 2
        assert len(outcome.finding_history[1]) == 0


class TestNonConvergence:
    def test_stubborn_agent_hits_empty_progress_detection(self):
        """Agent that doesn't respond to feedback — same error
        twice in a row → loop stops early via empty-progress
        detection."""
        pad = Scratchpad()

        def agent(_feedback):
            return SimpleNamespace(value="never improves")

        def critic(output):
            return [
                CriticFinding(
                    stage="logical",
                    severity="error",
                    message="same error every time",
                )
            ]

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=10,
        )

        # Stops at attempt 2 (empty-progress detected).
        assert outcome.attempts == 2
        assert outcome.passes is False
        # Both passes had the same finding.
        assert outcome.finding_history[0][0].message == outcome.finding_history[1][0].message
