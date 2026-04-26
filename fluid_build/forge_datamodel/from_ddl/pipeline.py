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

"""DDL-driven forge-data-model pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import CoordinatorResult, StageCoordinator
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator
from fluid_build.forge_datamodel.from_ddl.parser import DDLParser, TableDefinition


@dataclass
class DDLPipelineResult:
    coordinator: CoordinatorResult
    tables: List[TableDefinition]
    validation: object


def run_from_ddl(
    session: StageSession,
    *,
    name: str,
    ddl_texts: List[str],
    technique: str,
    source_type: Optional[str] = None,
    engine: str = "dbt",
) -> DDLPipelineResult:
    parser = DDLParser()
    tables: List[TableDefinition] = []
    for ddl_text in ddl_texts:
        tables.extend(parser.parse_ddl_content(ddl_text, dialect=source_type))
    if not tables:
        raise ValueError(
            "No CREATE TABLE statements were parsed from the supplied DDL. "
            "Check the dialect/source type or the DDL export format."
        )
    coordinator = StageCoordinator()
    result = coordinator.from_tables(
        session,
        name=name,
        tables=tables,
        technique=technique,
        source_type=source_type,
        engine=engine,
        include_physical=False,
    )
    validation = FluidContractValidator().validate(
        logical=result.logical,
        contract=result.contract,
        industry_pack=session.industry_pack,
    )
    return DDLPipelineResult(coordinator=result, tables=tables, validation=validation)
