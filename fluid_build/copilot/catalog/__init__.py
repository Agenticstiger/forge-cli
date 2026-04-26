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

"""Catalog adapters — read metadata from external data catalogs.

This module is the *reader* dual to ``fluid_build/providers/catalogs/``
(which is a *publisher* writing contracts to DMM / Splunk). Where the
provider-side catalogs publish forged contracts outward, this module's
adapters pull rich metadata IN from existing enterprise catalogs:
Snowflake Horizon, Databricks Unity, BigQuery + Dataplex, AWS Glue,
DataHub, Data Mesh Manager.

The metadata feeds every stage of the staged forge pipeline:

* **Logical** — entity names, descriptions, FK + lineage,
  classifications.
* **Builder** — owner, domain, sensitivity tags, sovereignty,
  quality SLAs land verbatim in the Fluid contract.
* **Transformation** — partition keys, quality rules, freshness SLAs
  become dbt configs / tests.

V1.5 design principles (from the plan's "four north stars"):

1. **World-class.** The :class:`CatalogAdapter` ABC is the public
   contract; community contributors add new catalogs by implementing
   it.
2. **Lightweight CLI.** Every catalog SDK is an *optional* extra,
   imported lazily inside each adapter so ``fluid --help`` stays
   sub-second.
3. **Best UX.** Errors carry a ``suggestions: list[str]`` field with
   the next-action operators need (e.g., the exact ``GRANT`` SQL).
4. **Open-community adoption.** Apache 2.0; cherry-picked SDK
   patterns are attributed in the adapter docstrings.

Public surface (the names community contributors and downstream
agents see):

* :class:`CatalogAdapter`            — the ABC.
* :class:`CatalogTable`              — Pydantic shape of one table.
* :class:`CatalogColumn`             — Pydantic shape of one column.
* :class:`CatalogLineage`            — upstream + downstream chains.
* :class:`CatalogScope`              — query scope for ``list_tables``.
* :class:`GlossaryTerm`              — business-glossary entry.
* :class:`SensitivityTag`            — typed PII / PHI / PCI flag.
* :class:`CatalogConnectionError`    — adapter couldn't reach the catalog.
* :class:`CatalogPermissionError`    — user lacks the privilege.
* :class:`CatalogConfigError`        — adapter config (env-var / SDK) is wrong.
"""

from __future__ import annotations

from fluid_build.copilot.catalog.base import (
    CatalogAdapter,
    CatalogConfigError,
    CatalogConnectionError,
    CatalogPermissionError,
)
from fluid_build.copilot.catalog.credentials import (
    BigQueryCredentials,
    CredentialNotFoundError,
    CredentialResolver,
    DataHubCredentials,
    DataMeshManagerCredentials,
    DataplexCredentials,
    GlueCredentials,
    SnowflakeCredentials,
    UnityCredentials,
)
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
    LineageRef,
    SensitivityTag,
)

__all__ = [
    # ABC + errors
    "CatalogAdapter",
    "CatalogConfigError",
    "CatalogConnectionError",
    "CatalogPermissionError",
    # Pydantic shapes
    "CatalogColumn",
    "CatalogForeignKey",
    "CatalogLineage",
    "CatalogScope",
    "CatalogTable",
    "GlossaryTerm",
    "LineageRef",
    "SensitivityTag",
    # Credential resolver + per-source typed credentials
    "CredentialNotFoundError",
    "CredentialResolver",
    "SnowflakeCredentials",
    "UnityCredentials",
    "BigQueryCredentials",
    "DataplexCredentials",
    "GlueCredentials",
    "DataHubCredentials",
    "DataMeshManagerCredentials",
]
