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

"""Coverage for structured stage feedback loops (Missing #4).

When the validator finds errors and the repair loop is about to
re-run a stage, the coordinator writes a ``StageFeedback`` to the
session scratchpad. The retried agent reads it and biases its
prompt accordingly — replaces the v1.0 "rerun with the same prompt
and hope" with a structured signal-passing protocol.

Tests pin:

1. The validator's findings are summarised into one
   ``StageFeedback.summary`` per failing stage.
2. The structured payload preserves every finding with
   ``severity``, ``message``, and ``field``.
3. Findings addressed to the failing stage land on
   ``scratchpad.feedback_for_stage(stage)``.
4. A clean report (no findings) produces no feedback.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import StageCoordinator
from fluid_build.copilot.schemas.stage_outputs import (
    ValidationFinding,
    ValidationReport,
)
from fluid_build.copilot.scratchpad import StageFeedback
from fluid_build.copilot.store.backends.null import NullBackend


def _session(tmp_path: Path) -> StageSession:
    return StageSession(store=NullBackend(), workspace_root=tmp_path)


class TestEmitValidatorFeedback:
    def test_produces_one_feedback_per_failing_stage(self, tmp_path):
        coordinator = StageCoordinator()
        session = _session(tmp_path)
        report = ValidationReport(
            score=4,
            issues=[
                ValidationFinding(
                    message="exposes is empty",
                    severity="error",
                    field="exposes",
                ),
                ValidationFinding(
                    message="metadata.domain is empty",
                    severity="warning",
                    field="metadata.domain",
                ),
            ],
            passes_schema=False,
        )

        coordinator._emit_validator_feedback(
            session,
            stage="builder",
            report=report,
        )

        scratch = session.get_scratchpad()
        feedback = scratch.feedback_for_stage("builder")
        assert len(feedback) == 1
        fb = feedback[0]
        assert fb.source_stage == "validator"
        assert fb.target_stage == "builder"
        assert "1 error" in fb.summary
        assert "1 warning" in fb.summary
        assert fb.structured["passes_schema"] is False

    def test_findings_payload_preserves_every_field(self, tmp_path):
        coordinator = StageCoordinator()
        session = _session(tmp_path)
        report = ValidationReport(
            score=2,
            issues=[
                ValidationFinding(
                    message="a",
                    severity="error",
                    field="x",
                ),
                ValidationFinding(
                    message="b",
                    severity="warning",
                    field="y",
                ),
                ValidationFinding(
                    message="c",
                    severity="info",
                    field="z",
                ),
            ],
            passes_schema=False,
        )

        coordinator._emit_validator_feedback(
            session,
            stage="logical",
            report=report,
        )

        feedback = session.get_scratchpad().feedback_for_stage("logical")
        findings = feedback[0].structured["findings"]
        assert len(findings) == 3
        assert findings[0] == {"message": "a", "severity": "error", "field": "x"}
        assert findings[1] == {"message": "b", "severity": "warning", "field": "y"}
        assert findings[2] == {"message": "c", "severity": "info", "field": "z"}

    def test_empty_report_produces_no_feedback(self, tmp_path):
        coordinator = StageCoordinator()
        session = _session(tmp_path)
        report = ValidationReport(score=10, issues=[], passes_schema=True)

        coordinator._emit_validator_feedback(
            session,
            stage="builder",
            report=report,
        )

        # No feedback because there's nothing to feed back.
        assert session.get_scratchpad().feedback == []

    def test_feedback_for_other_stage_isolated(self, tmp_path):
        """Feedback for ``builder`` does NOT show up when reading
        ``feedback_for_stage('logical')``. Pins the per-stage
        addressing contract."""
        coordinator = StageCoordinator()
        session = _session(tmp_path)
        report = ValidationReport(
            score=3,
            issues=[
                ValidationFinding(message="x", severity="error"),
            ],
            passes_schema=False,
        )

        coordinator._emit_validator_feedback(
            session,
            stage="builder",
            report=report,
        )

        scratch = session.get_scratchpad()
        assert len(scratch.feedback_for_stage("builder")) == 1
        assert scratch.feedback_for_stage("logical") == []
        assert scratch.feedback_for_stage("transformation") == []
