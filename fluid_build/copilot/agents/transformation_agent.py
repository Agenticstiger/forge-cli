# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""V2 transformation agent wrapper."""

from __future__ import annotations

from fluid_build.copilot.agents.builder_agent import BuilderAgent
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft, TransformPlan


class TransformationAgent:
    def __init__(self) -> None:
        self._builder = BuilderAgent()

    def run(self, logical: LogicalDraft, *, engine: str) -> TransformPlan:
        return self._builder._heuristic_transform_plan(logical, engine=engine)
