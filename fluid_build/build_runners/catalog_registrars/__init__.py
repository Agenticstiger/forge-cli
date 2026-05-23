# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Built-in catalog registrars satisfying ``api.catalog.CatalogRegistrar``.

Five targets covered (one module each):
- ``datahub``           — DataHub GMS REST
- ``openmetadata``      — OpenMetadata REST
- ``unity``             — Databricks Unity Catalog REST
- ``glue``              — AWS Glue Catalog (HTTP — no boto3 dependency in this layer)
- ``snowflake_horizon`` — Snowflake Horizon (HTTP RPC)

``build_registrar(target)`` constructs the registrar for a target from
environment configuration. Catalog endpoints and tokens are *deployment*
config, not contract content — the ``acquisitionCatalog`` schema block is
``additionalProperties: false`` and carries only the target *names* — so they
are resolved from the environment. The publish stage
(``cli/_acquisition_stage_ext.py``) calls ``build_registrar`` to populate the
``_catalog`` dispatcher registry before dispatching.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from fluid_build.api.catalog import CatalogRegistrar

from .datahub import DataHubRegistrar
from .glue import GlueCatalogRegistrar
from .openmetadata import OpenMetadataRegistrar
from .snowflake_horizon import SnowflakeHorizonRegistrar
from .unity import UnityCatalogRegistrar

__all__ = [
    "DataHubRegistrar",
    "GlueCatalogRegistrar",
    "OpenMetadataRegistrar",
    "SnowflakeHorizonRegistrar",
    "UnityCatalogRegistrar",
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
    if target == "unity":
        url = _env("FLUID_CATALOG_UNITY_URL", "DATABRICKS_HOST")
        if not url:
            return None
        return UnityCatalogRegistrar(
            base_url=url,
            **_kwargs(
                workspace_token=_env("FLUID_CATALOG_UNITY_TOKEN", "DATABRICKS_TOKEN"),
                catalog_name=_env("FLUID_CATALOG_UNITY_CATALOG"),
                schema_name=_env("FLUID_CATALOG_UNITY_SCHEMA"),
            ),
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
