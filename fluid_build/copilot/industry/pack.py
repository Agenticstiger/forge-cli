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

"""Typed industry pack — Pydantic surface over ``cli/industry_skills/*.yaml``.

The IndustryPack is **not** a replacement for the existing skills YAML
loader — it is a typed view over the same files plus optional
technique-specific seed skeletons under ``skeletons/<industry>/<technique>.yaml``.

Stages consume the pack structurally:

* Scaffold: reads ``canonical_model``, ``common_data_sources`` to pick
  template + detect industry.
* Modeler/Logical: reads ``seed_dv2_skeleton`` / ``seed_dimensional_skeleton``
  to start modeling from canonical-model shells, not from scratch.
* Validator: lints forged model names against ``canonical_model`` vocabulary.
* OSI emit: seeds ``ai_context.synonyms``/``examples`` from ``ai_context_seed``.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from fluid_build.copilot.schemas.data_model import DimensionalModel, DV2Model
from fluid_build.copilot.schemas.osi import OSIAIContext


class CanonicalModel(BaseModel):
    """Industry canonical data model reference (TMF SID, NRF ARTS, HL7 FHIR, ISO 20022)."""

    model_config = ConfigDict(populate_by_name=True)

    primary: str = ""
    label: str = ""
    description: str = ""
    supporting: List["CanonicalModel"] = Field(default_factory=list)


CanonicalModel.model_rebuild()


class IndustryDomain(BaseModel):
    """One sub-domain inside an industry (e.g. ``party_customer`` for telco)."""

    name: str
    label: str = ""
    description: str = ""
    key_entities: List[str] = Field(default_factory=list)


class ComplianceProfile(BaseModel):
    """Compliance regime for an industry — typed rather than free-text."""

    standards: List[str] = Field(default_factory=list)
    pii_handling: Optional[str] = None
    retention_years: Optional[int] = None
    regional_scope: List[str] = Field(default_factory=list)


class IndustryPack(BaseModel):
    """Typed industry knowledge unit consumed by staged agents."""

    name: str
    version: str = "1.0"
    label: str = ""
    description: str = ""
    canonical_model: CanonicalModel = Field(default_factory=CanonicalModel)
    domains: List[IndustryDomain] = Field(default_factory=list)
    compliance: ComplianceProfile = Field(default_factory=ComplianceProfile)
    common_data_sources: List[str] = Field(default_factory=list)
    ai_context_seed: OSIAIContext = Field(default_factory=OSIAIContext)
    seed_dv2_skeleton: Optional[DV2Model] = None
    seed_dimensional_skeleton: Optional[DimensionalModel] = None

    def cache_fingerprint(self) -> str:
        """Stable token used in LLM-stage cache keys — change-aware on version bumps."""
        return f"{self.name}@{self.version}"
