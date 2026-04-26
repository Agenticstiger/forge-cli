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
