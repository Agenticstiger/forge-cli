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

All five are auto-registered against the catalog dispatcher in
``build_runners/_catalog.py`` when ``HttpClient`` instantiation succeeds.
"""

from __future__ import annotations

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
]
