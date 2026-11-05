# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Business-intent-driven forge-data-model pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import CoordinatorResult, StageCoordinator
from fluid_build.copilot.schemas.intent import BusinessIntent
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator


@dataclass
class IntentPipelineResult:
    coordinator: CoordinatorResult
    validation: object


def run_from_intent(
    session: StageSession,
    *,
    intent: BusinessIntent,
    technique: str,
    engine: str = "dbt",
) -> IntentPipelineResult:
    coordinator = StageCoordinator()
    result = coordinator.from_intent(
        session,
        intent=intent,
        technique=technique,
        engine=engine,
        include_physical=False,
    )
    validation = FluidContractValidator().validate(
        logical=result.logical,
        contract=result.contract,
        industry_pack=session.industry_pack,
    )
    return IntentPipelineResult(coordinator=result, validation=validation)
