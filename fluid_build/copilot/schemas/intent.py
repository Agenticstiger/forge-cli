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

"""Business intent schemas ported from Model AI's agent input models."""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _coerce_to_string_list(value: Any) -> List[str]:
    """Accept either a list of bare strings or a list of ``{name: ...}`` dicts.

    The published docs show both forms in different examples.  Older
    callers wrote ``- customer`` while newer (interview-generated) ones
    write ``- name: customer``.  Both flow into the same downstream
    pipeline as a flat list of identifiers.
    """

    if value is None:
        return []
    if not isinstance(value, list):
        return value  # type: ignore[return-value]  # let pydantic raise
    out: List[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            ident = item.get("name") or item.get("id") or item.get("entity")
            if ident:
                out.append(str(ident))
            else:
                # Unknown dict shape — fall back to the first string value.
                for v in item.values():
                    if isinstance(v, str):
                        out.append(v)
                        break
        else:
            out.append(str(item))
    return out


class DataProduct(BaseModel):
    name: str
    domain: str
    description: str = ""
    owner: str = ""


class BusinessContext(BaseModel):
    problem_statement: str = ""
    decision_supported: str = ""
    consumer: str = ""
    consumer_hypothesis: str = ""
    reuse_hypothesis: str = ""


class Metric(BaseModel):
    """A metric definition.  Accepts either a full dict or a bare string name."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str = ""
    hint: str = ""

    @classmethod
    def _coerce_string(cls, value: Any) -> Any:
        # Allow ``- gross_revenue`` shorthand; downstream agents fill the rest.
        if isinstance(value, str):
            return {"name": value}
        return value


class Grain(BaseModel):
    description: str = ""
    entity: str
    time_dimension: str = ""


class Dimensions(BaseModel):
    entities: List[str] = Field(default_factory=list)
    attributes: List[str] = Field(default_factory=list)

    @field_validator("entities", "attributes", mode="before")
    @classmethod
    def _accept_dicts_or_strings(cls, value: Any) -> List[str]:
        return _coerce_to_string_list(value)


class DataSource(BaseModel):
    source_name: str
    source_type: str
    description: str = ""


class ColumnMetadata(BaseModel):
    name: str
    data_type: str
    semantic_tag: str = ""


class TableMetadata(BaseModel):
    table_name: str
    table_type: str = "table"
    columns: List[ColumnMetadata] = Field(default_factory=list)


class Relationship(BaseModel):
    relationship_type: str
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    confidence: str = ""


class ColumnProfile(BaseModel):
    column_name: str
    distinct_count: Optional[int] = None
    null_percentage: Optional[float] = None
    avg_value: Optional[float] = None
    data_distribution: Optional[str] = None


class ProfilingStats(BaseModel):
    table_name: str
    column_profiles: List[ColumnProfile] = Field(default_factory=list)


class AdpMetadata(BaseModel):
    tables: List[TableMetadata] = Field(default_factory=list)
    relationships: List[Relationship] = Field(default_factory=list)
    profiling_stats: List[ProfilingStats] = Field(default_factory=list)


class Consumption(BaseModel):
    use_cases: List[str] = Field(default_factory=list)
    output_format: List[str] = Field(default_factory=list)
    refresh_frequency: str = ""


class ModelingPreferences(BaseModel):
    technique: Optional[str] = None
    grain: Optional[Grain] = None
    scd_policy_default: Optional[str] = None
    hash_key_algorithm: Optional[str] = None


class BusinessIntent(BaseModel):
    """Canonical business intent used by ``forge data-model from-intent``."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = ""
    message_id: str = ""
    interaction_type: str = "from-intent"
    data_product: DataProduct
    business_context: BusinessContext = Field(default_factory=BusinessContext)
    metrics: List[Metric] = Field(default_factory=list)
    grain: Optional[Grain] = None
    dimensions: Dimensions = Field(default_factory=Dimensions)
    data_sources: List[DataSource] = Field(default_factory=list)
    adp_metadata: AdpMetadata = Field(default_factory=AdpMetadata)
    consumption: Consumption = Field(default_factory=Consumption)
    business_rules: List[str] = Field(default_factory=list)
    modeling: Optional[ModelingPreferences] = None

    @field_validator("metrics", mode="before")
    @classmethod
    def _accept_metric_strings(cls, value: Any) -> Any:
        """Accept ``metrics: [gross_revenue, refunds]`` shorthand alongside
        the canonical ``[{name: gross_revenue, description: ...}]`` form."""

        if not isinstance(value, list):
            return value
        coerced: List[Any] = []
        for item in value:
            if isinstance(item, str):
                coerced.append({"name": item})
            else:
                coerced.append(item)
        return coerced


AgentInput = BusinessIntent
