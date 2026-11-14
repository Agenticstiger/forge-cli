# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Forge data-model pipeline driven by a metadata-source catalog.

V1.5 entry point — parallel to ``from_intent`` and ``from_ddl``. Reads
table / column / lineage / glossary metadata from the user's
configured catalog (Snowflake Horizon, Databricks Unity, BigQuery +
Dataplex, AWS Glue, DataHub, Data Mesh Manager) and runs the staged
forge pipeline against the resulting :class:`LogicalDraft`.

User-facing CLI: ``fluid forge data-model from-source ...``.
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
