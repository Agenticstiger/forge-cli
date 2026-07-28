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

"""Built-in catalog registrars — every backend consumes
:class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`.

Three targets covered (one module each):

- ``datahub``           — DataHub GMS REST + MCP (DataProduct + Domain + Datasets)
- ``openmetadata``      — OpenMetadata REST (Tables + extension)
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

**Retired targets** — the previous ``glue`` and ``snowflake_horizon``
registrars were folded into the IaC plugins (``fluid_build/iac/providers/aws.py``
+ ``fluid_build/iac/providers/snowflake.py``). The same metadata that the
registrars used to push via ``glue:UpdateTable`` / Snowsight REST is now
emitted into ``aws_glue_catalog_table`` and ``snowflake_table`` resources
directly — one source of truth, one ``tofu apply``, drift detection for
free. Contracts that listed ``glue`` or ``snowflake_horizon`` under
``properties.catalog.register`` will get a "not configured" result from
``build_registrar``; users should drop those targets from the contract and
use ``fluid apply --engine opentofu`` to manage the catalog metadata.

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
from .openmetadata import OpenMetadataRegistrar

__all__ = [
    "DataHubRegistrar",
    "DataMeshManagerRegistrar",
    "OpenMetadataRegistrar",
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
    ecosystem variable (DataHub's ``DATAHUB_GMS_URL``) it is honoured as a
    fallback so an operator already in that environment needs zero extra
    config.

    ``glue`` and ``snowflake_horizon`` were retired in favour of the IaC
    plugins — see the module docstring. Asking for them here returns
    ``None`` and the dispatcher records the standard "no registrar
    configured" result; users should remove those targets from the
    contract.
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
    return None
