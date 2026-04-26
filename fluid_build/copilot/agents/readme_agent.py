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
