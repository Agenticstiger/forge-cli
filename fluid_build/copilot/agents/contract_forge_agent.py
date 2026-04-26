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

"""Contract forge agent: LogicalDraft -> Fluid contract."""

from __future__ import annotations

import json
from typing import Any, Dict

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical


class ContractForgeAgent:
    """Own the deterministic contract-forging boundary.

    The modeler/logical stage may be LLM-backed or heuristic, but the final
    Fluid contract is assembled here so the artifact carries explicit evidence
    about strictness, provider identity, and any fallback that occurred.
    """

    stage = "contract_forge"

    def forge_contract(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
        engine: str = "dbt",
    ) -> Dict[str, Any]:
        # Item 7 — pass scratchpad annotations through so the
        # emitted contract carries per-claim provenance under
        # ``metadata.labels.provenance``. Best-effort: missing
        # scratchpad / annotations just produces a contract
        # without the provenance block.
        annotations = None
        try:
            annotations = session.get_scratchpad().get_annotations()
        except Exception:  # pragma: no cover — defensive
            pass
        contract = build_contract_from_logical(
            logical,
            build_engine=engine,
            annotations=annotations,
        )
        labels = contract.setdefault("labels", {})
        session.record_agent_event(
            stage="contract",
            agent=type(self).__name__,
            mode="deterministic",
        )
        labels["contractForgedBy"] = type(self).__name__
        labels["agenticStageManifest"] = self._stage_manifest(session)
        labels["agenticMode"] = self._agentic_mode(session)
        labels["agenticStrictLlmRequired"] = "true" if session.require_llm else "false"
        labels["agenticFallbackUsed"] = "true" if session.fallback_used else "false"
        labels["agenticRepairUsed"] = "true" if session.repair_used else "false"
        if session.fallback_events:
            labels["agenticFallbackStages"] = ",".join(
                sorted(
                    {
                        event.get("stage", "")
                        for event in session.fallback_events
                        if event.get("stage")
                    }
                )
            )
            labels["agenticFallbackReasons"] = ",".join(
                sorted(
                    {
                        self._fallback_reason(event)
                        for event in session.fallback_events
                        if event.get("reason") or event.get("error_type")
                    }
                )
            )
        if session.repair_events:
            labels["agenticRepairStages"] = ",".join(
                sorted(
                    {
                        event.get("stage", "")
                        for event in session.repair_events
                        if event.get("stage")
                    }
                )
            )
            labels["agenticRepairReasons"] = ",".join(
                sorted(
                    {
                        self._fallback_reason(event)
                        for event in session.repair_events
                        if event.get("reason") or event.get("error_type")
                    }
                )
            )
            details = sorted(
                {
                    str(event.get("detail", "")).strip()
                    for event in session.repair_events
                    if event.get("detail")
                }
            )
            if details:
                labels["agenticRepairDetails"] = " | ".join(details)[:1000]
        self._annotate_llm(labels, session)
        return contract

    def _agentic_mode(self, session: StageSession) -> str:
        if session.require_llm:
            return "strict_llm"
        if session.llm_config is not None:
            return "llm_with_fallback"
        return "heuristic"

    def _annotate_llm(self, labels: Dict[str, Any], session: StageSession) -> None:
        config = session.llm_config
        if config is None:
            return
        provider = session.active_provider or getattr(config, "provider", None)
        model = getattr(config, "model", None)
        model_source = getattr(config, "model_source", None)
        if provider:
            labels["llmProvider"] = str(provider)
        if model:
            labels["llmModel"] = str(model)
        if model_source:
            labels["llmModelSource"] = str(model_source)

    def _stage_manifest(self, session: StageSession) -> str:
        """Return a compact JSON stage-owner manifest for auditability."""
        events = list(session.agent_events)
        if not events:
            events = [
                {
                    "stage": "contract",
                    "agent": type(self).__name__,
                    "mode": "deterministic",
                    "status": "completed",
                }
            ]
        return json.dumps(events, sort_keys=True, separators=(",", ":"))[:2000]

    def _fallback_reason(self, event: Dict[str, str]) -> str:
        reason = event.get("reason", "")
        error_type = event.get("error_type", "")
        if reason and error_type:
            return f"{reason}:{error_type}"
        return reason or error_type
