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

"""Forge data-model pipeline driven by a metadata-source catalog.

V1.5 entry point — parallel to ``from_intent`` and ``from_ddl``. Reads
table / column / lineage / glossary metadata from the user's
configured catalog (Snowflake Horizon, Databricks Unity, BigQuery +
Dataplex, AWS Glue, DataHub, Data Mesh Manager) and runs the staged
forge pipeline against the resulting :class:`LogicalDraft`.

User-facing CLI: ``fluid forge data-model from-source ...``.
MCP-tool: ``forge_from_source``.
Pipeline coordinator: ``StageCoordinator.from_catalog``.

The "source" vocabulary in user-facing CLI / MCP / config disambiguates
this read role from the existing publish-target catalog role at
``fluid_build.providers.catalogs``.
"""

from fluid_build.forge_datamodel.from_catalog.pipeline import (
    CatalogPipelineResult,
    run_from_catalog,
)

__all__ = ["CatalogPipelineResult", "run_from_catalog"]
