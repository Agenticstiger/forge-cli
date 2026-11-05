# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""V2 readme agent wrapper."""

from __future__ import annotations

from fluid_build.copilot.agents.builder_agent import BuilderAgent
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft, ReadmeDraft


class ReadmeAgent:
    def __init__(self) -> None:
        self._builder = BuilderAgent()

    def run(self, logical: LogicalDraft, *, engine: str) -> ReadmeDraft:
        return ReadmeDraft(
            readme_markdown=self._builder._build_readme(logical, engine=engine),
            description=f"Generated README for {logical.name}",
        )
