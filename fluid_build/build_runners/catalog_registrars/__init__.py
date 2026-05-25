# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Built-in catalog registrars — every backend consumes
:class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`.

Five targets covered (one module each):

- ``datahub``           — DataHub GMS REST + MCP (DataProduct + Domain + Datasets)
- ``openmetadata``      — OpenMetadata REST (Tables + extension)
- ``glue``              — AWS Glue Data Catalog (Tables + Parameters)
- ``snowflake_horizon`` — Snowflake Horizon (Tables + markdown comments)
- ``datamesh_manager``  — Data Mesh Manager / Entropy Data
                          (PUT /data-products in ODPS + /datacontracts in ODCS per asset)

Importing this package triggers each module's ``register_catalog_backend(...)``
side-effect so ``fluid publish --target <name>`` and contract
``properties.catalog.register: [<name>]`` both resolve through a single
declaration site.

``build_registrar(target)`` constructs the registrar for a target from
environment configuration. Catalog endpoints and tokens are *deployment*
config, not contract content — the ``acquisitionCatalog`` schema block is
``additionalProperties: false`` and carries only the target *names* — so they
are resolved from the environment. The publish stage
(``cli/_acquisition_stage_ext.py``) calls ``build_registrar`` to populate the
``_catalog`` dispatcher registry before dispatching.

A Databricks Unity Catalog *read* adapter still lives under
``fluid_build/copilot/catalog/unity.py`` for ``forge``-side discovery;
the publish-side registrar was dropped because the OSS Unity Catalog
server's strict v0.4+ table-create validation made round-tripping the
canonical payload too fragile for a generic publish path. Databricks-
hosted UC remains addressable via the upstream Databricks SDK if
needed in the future.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from fluid_build.api.catalog import CatalogRegistrar

from .datahub import DataHubRegistrar
from .datamesh_manager import DataMeshManagerRegistrar
from .glue import GlueCatalogRegistrar
from .openmetadata import OpenMetadataRegistrar
from .snowflake_horizon import SnowflakeHorizonRegistrar

__all__ = [
    "DataHubRegistrar",
    "DataMeshManagerRegistrar",
    "GlueCatalogRegistrar",
    "OpenMetadataRegistrar",
    "SnowflakeHorizonRegistrar",
    "build_registrar",
]


def _env(*names: str) -> Optional[str]:
    """First non-empty value among ``names`` in the environment, else None."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return None


def _kwargs(**maybe: Optional[str]) -> Dict[str, str]:
    """Keep only the kwargs with a truthy value, so dataclass field defaults
    apply for everything left unset."""
    return {k: v for k, v in maybe.items() if v}


def build_registrar(target: str) -> Optional[CatalogRegistrar]:
    """Construct a built-in registrar for ``target`` from environment config.

    Returns ``None`` when the target's required endpoint is unset — the
    dispatcher then records a clear "not configured" result instead of
    dialling a placeholder host. Where a target has a well-established
    ecosystem variable (DataHub's ``DATAHUB_GMS_URL``, Databricks'
    ``DATABRICKS_HOST``, AWS' ``AWS_REGION``) it is honoured as a fallback so
    an operator already in that environment needs zero extra config.
    """
    if target == "datahub":
        url = _env("FLUID_CATALOG_DATAHUB_URL", "DATAHUB_GMS_URL")
        if not url:
            return None
        return DataHubRegistrar(
            base_url=url,
            api_token=_env("FLUID_CATALOG_DATAHUB_TOKEN", "DATAHUB_GMS_TOKEN"),
        )
    if target == "openmetadata":
        url = _env("FLUID_CATALOG_OPENMETADATA_URL")
        if not url:
            return None
        return OpenMetadataRegistrar(
            base_url=url,
            api_token=_env("FLUID_CATALOG_OPENMETADATA_TOKEN"),
        )
    if target == "glue":
        region = _env("FLUID_CATALOG_GLUE_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
        if not region:
            return None
        return GlueCatalogRegistrar(
            region=region,
            **_kwargs(
                catalog_id=_env("FLUID_CATALOG_GLUE_CATALOG_ID"),
                database_name=_env("FLUID_CATALOG_GLUE_DATABASE"),
            ),
        )
    if target == "snowflake_horizon":
        url = _env("FLUID_CATALOG_SNOWFLAKE_URL")
        if not url:
            return None
        return SnowflakeHorizonRegistrar(
            account_url=url,
            **_kwargs(
                auth_token=_env("FLUID_CATALOG_SNOWFLAKE_TOKEN"),
                database=_env("FLUID_CATALOG_SNOWFLAKE_DATABASE"),
                schema=_env("FLUID_CATALOG_SNOWFLAKE_SCHEMA"),
            ),
        )
    return None
