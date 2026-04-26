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

"""Sprint #2 + #3 + Sprint #1 pin — scratchpad signals reach the
prompts and provenance.

These tests assert the ACTUAL wiring (not just the helpers). Without
them, a future refactor could remove the inject calls and the rest
of the agentic pipeline would silently lose signal again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.builder_agent import BuilderAgent
from fluid_build.copilot.agents.modeler_agent import _inject_scratchpad_signals
from fluid_build.copilot.schemas.data_model import DV2Model
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import (
    ConceptualDraft,
    LogicalDraft,
)
from fluid_build.copilot.scratchpad import (
    CriticFinding,
    StageFeedback,
)
from fluid_build.copilot.store.backends.null import NullBackend


def _session(tmp_path: Path) -> StageSession:
    return StageSession(store=NullBackend(), workspace_root=tmp_path)


def _logical() -> LogicalDraft:
    return LogicalDraft(
        name="orders",
        technique="data_vault_2",
        conceptual=ConceptualDraft(
            name="orders",
            description="Orders",
            entities=[],
            relationships=[],
        ),
        osi=OSISemanticModel(
            name="orders",
            description="Orders",
            ai_context=OSIAIContext(instructions="Use for revenue."),
            datasets=[],
            relationships=[],
            metrics=[],
        ),
        dv2=DV2Model(hubs=[], links=[], satellites=[], pits=[], bridges=[]),
    )


class TestModelerPromptInjection:
    """Sprint #2 — validator stage feedback lands in the modeler's
    next prompt.

    Sprint #3 — critic findings on the logical stage land in the
    same prompt so the modeler sees both signals at once on retry."""

    def test_critic_findings_reach_payload(self, tmp_path):
        session = _session(tmp_path)
        scratchpad = session.get_scratchpad()
        scratchpad.add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="warning",
                message="hub_customer has no business_key_columns",
                suggestion="set business_key_columns to ['customer_id']",
                target="dv2.hubs.hub_customer.business_key_columns",
            )
        )

        payload: dict[str, Any] = {"name": "orders"}
        _inject_scratchpad_signals(
            session,
            payload=payload,
            target_stages=("logical", "modeler"),
        )

        assert "critic_findings" in payload
        assert len(payload["critic_findings"]) == 1
        finding = payload["critic_findings"][0]
        assert finding["severity"] == "warning"
        assert "business_key_columns" in finding["message"]
        assert "customer_id" in finding["suggestion"]

    def test_validator_feedback_reaches_payload(self, tmp_path):
        session = _session(tmp_path)
        scratchpad = session.get_scratchpad()
        scratchpad.add_feedback(
            StageFeedback(
                source_stage="validator",
                target_stage="logical",
                summary="3 errors in dv2 layer; fix on next attempt",
                structured={"score": 4, "errors": 3},
            )
        )

        payload: dict[str, Any] = {"name": "orders"}
        _inject_scratchpad_signals(
            session,
            payload=payload,
            target_stages=("logical", "modeler"),
        )

        assert "validator_feedback" in payload
        assert payload["validator_feedback"][0]["source_stage"] == "validator"
        assert "3 errors" in payload["validator_feedback"][0]["summary"]
        assert payload["validator_feedback"][0]["structured"]["errors"] == 3

    def test_clean_scratchpad_no_payload_keys(self, tmp_path):
        """No findings → no extra payload keys. Defends against
        a regression that leaves empty arrays in the prompt and
        confuses the LLM."""
        session = _session(tmp_path)
        payload: dict[str, Any] = {"name": "orders"}
        _inject_scratchpad_signals(
            session,
            payload=payload,
            target_stages=("logical", "modeler"),
        )
        assert "critic_findings" not in payload
        assert "validator_feedback" not in payload

    def test_target_stage_filter_excludes_other_stages(self, tmp_path):
        """Findings addressed to OTHER stages must not leak into
        this prompt. ``target_stages=("logical", "modeler")`` must
        ONLY pull findings keyed to those two stages."""
        session = _session(tmp_path)
        scratchpad = session.get_scratchpad()
        # Builder finding — should NOT be visible to logical.
        scratchpad.add_critic_finding(
            CriticFinding(
                stage="builder",
                severity="error",
                message="contract has no exposes",
            )
        )
        # Logical finding — SHOULD be visible.
        scratchpad.add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="warning",
                message="missing keys",
            )
        )

        payload: dict[str, Any] = {"name": "x"}
        _inject_scratchpad_signals(
            session,
            payload=payload,
            target_stages=("logical",),
        )
        assert len(payload.get("critic_findings", [])) == 1
        assert payload["critic_findings"][0]["message"] == "missing keys"


class TestBuilderProvenance:
    """Sprint #3 — BuilderAgent surfaces scratchpad signals on
    ``physical.provenance`` so downstream code (cost summary,
    audit trail, receipt writer) sees the critic's voice."""

    def test_critic_findings_in_provenance(self, tmp_path):
        session = _session(tmp_path)
        scratchpad = session.get_scratchpad()
        scratchpad.add_critic_finding(
            CriticFinding(
                stage="builder",
                severity="error",
                message="Contract has no exposes",
                target="exposes",
            )
        )

        physical = BuilderAgent().build_physical(
            session,
            logical=_logical(),
            contract={"metadata": {}, "exposes": [{"name": "orders"}]},
            engine="dbt",
        )

        prov = physical.provenance or {}
        assert "critic_findings" in prov
        assert prov["critic_findings"][0]["severity"] == "error"
        assert "exposes" in prov["critic_findings"][0]["message"]

    def test_validator_feedback_in_provenance(self, tmp_path):
        session = _session(tmp_path)
        scratchpad = session.get_scratchpad()
        scratchpad.add_feedback(
            StageFeedback(
                source_stage="validator",
                target_stage="builder",
                summary="2 errors found; bias next attempt",
            )
        )

        physical = BuilderAgent().build_physical(
            session,
            logical=_logical(),
            contract={"metadata": {}, "exposes": [{"name": "orders"}]},
            engine="dbt",
        )

        prov = physical.provenance or {}
        assert "validator_feedback" in prov
        assert "2 errors" in prov["validator_feedback"][0]["summary"]

    def test_no_signals_no_extra_provenance_keys(self, tmp_path):
        """Clean scratchpad → no extra provenance keys. Receipt
        writers downstream check for the keys; we don't want
        to lie with empty arrays."""
        session = _session(tmp_path)
        physical = BuilderAgent().build_physical(
            session,
            logical=_logical(),
            contract={"metadata": {}, "exposes": [{"name": "orders"}]},
            engine="dbt",
        )
        prov = physical.provenance or {}
        assert "critic_findings" not in prov
        assert "validator_feedback" not in prov
