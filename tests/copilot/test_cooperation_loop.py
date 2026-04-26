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

"""Coverage for the multi-turn agent cooperation loop (E10)."""

from __future__ import annotations

from typing import List, Optional

import pytest

from fluid_build.copilot.agents.cooperation_loop import (
    CooperationOutcome,
    run_with_critic_loop,
)
from fluid_build.copilot.events import Event, get_event_bus, reset_event_bus
from fluid_build.copilot.scratchpad import (
    CriticFinding,
    Scratchpad,
    StageFeedback,
)


@pytest.fixture(autouse=True)
def _hermetic():
    reset_event_bus()
    yield
    reset_event_bus()


class TestConvergenceOnSecondPass:
    def test_loop_exits_when_critic_clean(self):
        """Pass 1: critic finds 1 error. Agent sees feedback,
        produces clean output. Pass 2: critic clean → loop exits
        with ``passes=True``."""
        pad = Scratchpad()
        attempts: List[Optional[StageFeedback]] = []

        def agent(feedback):
            attempts.append(feedback)
            # Output is just the attempt number for testability.
            return f"attempt_{len(attempts)}"

        # Critic: errors on first call only.
        call_count = {"n": 0}

        def critic(output):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message="missing business key",
                    )
                ]
            return []  # clean on retry

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
        )

        assert outcome.attempts == 2
        assert outcome.passes is True
        assert outcome.output == "attempt_2"
        # First attempt had no feedback; second had the critic's.
        assert attempts[0] is None
        assert isinstance(attempts[1], StageFeedback)
        assert attempts[1].target_stage == "logical"
        assert attempts[1].source_stage == "critic"

    def test_clean_first_pass_no_loop(self):
        pad = Scratchpad()

        def agent(feedback):
            return "clean"

        def critic(output):
            return []

        outcome = run_with_critic_loop(
            stage="builder",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
        )
        assert outcome.attempts == 1
        assert outcome.passes is True


class TestCappedAttempts:
    def test_loop_exits_at_max_attempts(self):
        """Critic always errors → loop exits at cap with
        ``passes=False``."""
        pad = Scratchpad()

        def agent(feedback):
            return "stubborn"

        # Each call returns a NEW error message so empty-progress
        # detection doesn't kick in early.
        call_count = {"n": 0}

        def critic(output):
            call_count["n"] += 1
            return [
                CriticFinding(
                    stage="logical",
                    severity="error",
                    message=f"error #{call_count['n']}",
                )
            ]

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=3,
        )
        assert outcome.attempts == 3
        assert outcome.passes is False

    def test_max_attempts_one_means_no_loop(self):
        pad = Scratchpad()

        def agent(feedback):
            return "x"

        def critic(output):
            return [CriticFinding(stage="x", severity="error", message="m")]

        outcome = run_with_critic_loop(
            stage="x",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=1,
        )
        assert outcome.attempts == 1


class TestEmptyProgressDetection:
    def test_same_error_set_twice_stops_early(self):
        """Agent isn't responding to feedback (same errors twice
        in a row). Loop stops to avoid burning tokens."""
        pad = Scratchpad()

        def agent(feedback):
            return "no progress"

        def critic(output):
            # Always the SAME message — empty-progress signature.
            return [
                CriticFinding(
                    stage="logical",
                    severity="error",
                    message="business key missing",
                )
            ]

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=10,
        )
        # Stops after 2 attempts (same signature seen twice) even
        # though max_attempts=10.
        assert outcome.attempts == 2
        assert outcome.passes is False


class TestEventEmission:
    def test_each_attempt_emits_event(self):
        pad = Scratchpad()
        received: List[Event] = []
        get_event_bus().subscribe(received.append)

        def agent(feedback):
            return "x"

        def critic(output):
            return [CriticFinding(stage="x", severity="warning", message="w")]

        run_with_critic_loop(
            stage="x",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=2,
        )
        cooperation_events = [e for e in received if e.event_type == "cooperation.attempt"]
        # Loop exited at attempt 1 because there were no ERRORS
        # (only warnings) — so one event.
        assert len(cooperation_events) >= 1
        assert cooperation_events[0].payload["stage"] == "x"
        assert cooperation_events[0].payload["attempt"] == 1
        assert cooperation_events[0].payload["warning_count"] == 1


class TestScratchpadAccumulation:
    def test_findings_land_on_scratchpad(self):
        pad = Scratchpad()
        # First attempt: 1 error. Second: 1 different error.
        call_count = {"n": 0}
        errors = [
            "first error",
            "second error",
        ]

        def agent(feedback):
            return "x"

        def critic(output):
            call_count["n"] += 1
            idx = call_count["n"] - 1
            if idx >= len(errors):
                return []
            return [
                CriticFinding(
                    stage="logical",
                    severity="error",
                    message=errors[idx],
                )
            ]

        run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=3,
        )
        # All findings from each pass land on the scratchpad.
        assert len(pad.critic_findings) == 2
        # Two pieces of stage feedback (one per attempt with errors).
        assert len(pad.feedback) == 2

    def test_warning_only_findings_dont_loop(self):
        """Warnings don't trigger another iteration — only errors
        do. Loop converges immediately."""
        pad = Scratchpad()

        def agent(feedback):
            return "x"

        def critic(output):
            return [
                CriticFinding(
                    stage="logical",
                    severity="warning",
                    message="cosmetic concern",
                )
            ]

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=agent,
            critic_callable=critic,
            scratchpad=pad,
            max_attempts=5,
        )
        assert outcome.attempts == 1
        assert outcome.passes is True
        assert len(pad.critic_findings) == 1


class TestInputValidation:
    def test_zero_max_attempts_rejected(self):
        with pytest.raises(ValueError):
            run_with_critic_loop(
                stage="x",
                agent_callable=lambda f: "x",
                critic_callable=lambda o: [],
                scratchpad=Scratchpad(),
                max_attempts=0,
            )
