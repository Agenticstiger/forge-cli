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

"""Catalog-driven forge-data-model pipeline (V1.5).

Mirrors the structure of ``from_intent.pipeline`` and
``from_ddl.pipeline`` so every entry point looks the same to
downstream callers (the CLI dispatcher, the MCP forge_from_source
tool, the AI-mode interview branch).

Three steps:

1. Resolve the catalog adapter via the credential resolver.
2. Run :meth:`StageCoordinator.from_catalog` (which calls
   :meth:`LogicalAgent.from_catalog` and then the rest of the
   staged pipeline).
3. Validate the emitted contract.

The CLI surface (``fluid forge data-model from-source``) wraps this
pipeline and adds the file-write + sidecar-emit steps that
``run_from_intent`` / ``run_from_ddl`` already do via
``forge_data_model._write_or_report``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import CoordinatorResult, StageCoordinator
from fluid_build.copilot.catalog.base import CatalogAdapter
from fluid_build.copilot.catalog.models import CatalogScope
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator


@dataclass
class CatalogPipelineResult:
    """Same shape as ``IntentPipelineResult`` — coordinator output +
    validation report. Keeps the CLI dispatcher's
    ``_write_or_report`` happy without special-casing the catalog
    path."""

    coordinator: CoordinatorResult
    validation: object


def run_from_catalog(
    session: StageSession,
    *,
    name: str,
    adapter: CatalogAdapter,
    scope: CatalogScope,
    technique: str,
    engine: str = "dbt",
) -> CatalogPipelineResult:
    """Run the staged forge pipeline against a catalog scope.

    Parameters
    ----------
    session:
        Standard staged-pipeline session — store, llm_config,
        memory namespaces.
    name:
        Output model name (used for the sidecar filename and the
        contract's ``id`` / ``name``).
    adapter:
        Constructed :class:`CatalogAdapter` (typically built via
        ``<Adapter>.from_resolver`` at the dispatch layer).
    scope:
        Database / schema / catalog scope inside the source
        catalog.
    technique:
        ``"data_vault_2"`` or ``"dimensional"``.
    engine:
        Build engine for the emitted contract (``"dbt"`` /
        ``"sql"`` / etc.).

    Returns
    -------
    :class:`CatalogPipelineResult` carrying the coordinator output
    and a ``ValidationReport``.
    """
    coordinator = StageCoordinator()
    result = coordinator.from_catalog(
        session,
        name=name,
        adapter=adapter,
        scope=scope,
        technique=technique,
        engine=engine,
        include_physical=False,
    )
    validation = FluidContractValidator().validate(
        logical=result.logical,
        contract=result.contract,
        industry_pack=session.industry_pack,
    )
    return CatalogPipelineResult(coordinator=result, validation=validation)
