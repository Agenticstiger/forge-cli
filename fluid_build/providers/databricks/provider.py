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

"""Databricks provider — plans only; ``fluid apply`` provisions through OpenTofu.

Registered so ``--provider databricks``, ``fluid providers`` and ``fluid diff``
know the platform (an IaC-only cloud is rejected by all three). It imports no
Databricks SDK: :meth:`DatabricksProvider.plan` lists the objects the OpenTofu
module manages (``iac/providers/databricks.py::planned_objects``), so the plan and
the apply cannot disagree. Nothing here talks to a workspace.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..base import ApplyResult, BaseProvider, ProviderError, ProviderMetadata

_METADATA = ProviderMetadata(
    name="databricks",
    display_name="Databricks",
    description=(
        "Unity Catalog schemas, managed Delta tables and grants, applied through OpenTofu "
        "(databricks/databricks provider)"
    ),
    supported_platforms=["databricks"],
    tags=["databricks", "unity-catalog", "delta", "opentofu"],
)


class DatabricksProvider(BaseProvider):
    """Plan-only provider for Databricks Unity Catalog."""

    name = "databricks"
    #: ``fluid apply`` reaches Databricks through the OpenTofu engine only, so the
    #: native simple apply never takes this platform (``cli/apply.py``).
    native_apply = False

    @classmethod
    def get_provider_info(cls) -> ProviderMetadata:
        return replace(_METADATA)

    def plan(
        self, contract: Mapping[str, Any], *, mode: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        from fluid_build.iac.providers.databricks import planned_objects

        return planned_objects(contract)

    def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
        raise ProviderError(
            "the databricks provider has no native apply: `fluid apply` provisions "
            "Databricks through the OpenTofu engine"
        )
