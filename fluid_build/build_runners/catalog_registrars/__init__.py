# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Built-in catalog registrars — every backend consumes
:class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`.

Six targets covered (one module each):

- ``datahub``           — DataHub GMS REST + MCP (DataProduct + Domain + Datasets)
- ``openmetadata``      — OpenMetadata REST (Tables + extension)
- ``unity``             — Databricks Unity Catalog REST (Tables + properties)
- ``glue``              — AWS Glue Data Catalog (Tables + Parameters)
- ``snowflake_horizon`` — Snowflake Horizon (Tables + markdown comments)
- ``datamesh_manager``  — Data Mesh Manager / Entropy Data
                          (PUT /data-products in ODPS + /datacontracts in ODCS per asset)

Importing this package triggers each module's ``register_catalog_backend(...)``
side-effect so ``fluid publish --target <name>`` and contract
``properties.catalog.register: [<name>]`` both resolve through a single
declaration site.
"""

from __future__ import annotations

from .datahub import DataHubRegistrar
from .datamesh_manager import DataMeshManagerRegistrar
from .glue import GlueCatalogRegistrar
from .openmetadata import OpenMetadataRegistrar
from .snowflake_horizon import SnowflakeHorizonRegistrar
from .unity import UnityCatalogRegistrar

__all__ = [
    "DataHubRegistrar",
    "DataMeshManagerRegistrar",
    "GlueCatalogRegistrar",
    "OpenMetadataRegistrar",
    "SnowflakeHorizonRegistrar",
    "UnityCatalogRegistrar",
]
