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

"""Data-model schemas shared across staged forge flows."""

from __future__ import annotations

from typing import Any, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

TechniqueLiteral = Literal["data_vault_2", "dimensional"]

DimensionalVariant = Literal["star", "snowflake", "galaxy", "flat"]
"""Canonical Kimball flavors forge-cli supports.

* **star** — one fact table joined to denormalized dimension tables.
  The default; the classical single-star Kimball shape.
* **snowflake** — dimensions are normalized into multiple levels; better
  storage, slightly more complex SQL.
* **galaxy** — multiple fact tables sharing conformed dimensions.
  Appropriate for data products spanning several business processes.
* **flat** — one wide table (a.k.a. One Big Table / OBT); fact and
  dimension columns merged for BI consumption.

Adding a new variant means (1) appending here, (2) extending
:data:`DIMENSIONAL_VARIANTS`, and (3) giving
``forge_datamodel/emit/variants.py`` a rendering rule for it. Tests in
``test_dimensional_variant_ir.py`` enforce the tuple stays aligned with
the Literal.
"""

DIMENSIONAL_VARIANTS: Tuple[str, ...] = ("star", "snowflake", "galaxy", "flat")
"""Tuple form of :data:`DimensionalVariant` — usable at runtime for
iteration (emit-all-variants flag) without retyping the list. Must stay
aligned with the Literal; the test suite asserts the two agree."""


class FieldDefinition(BaseModel):
    name: str
    data_type: str
    description: Optional[str] = None
    nullable: bool = True
    source_columns: List[str] = Field(default_factory=list)


class HashKeyStrategy(BaseModel):
    algorithm: Literal["md5", "sha256"] = "md5"
    delimiter: str = "||"
    null_token: str = "__NULL__"
    upper_case: bool = True

    @model_validator(mode="before")
    @classmethod
    def _coerce_from_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"algorithm": value}
        return value


class HubDefinition(BaseModel):
    entity_name: str
    hub_table_name: str
    business_key_columns: List[str] = Field(default_factory=list)
    mapped_source_tables: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class JoinKeyDetail(BaseModel):
    table1: str
    column1: str
    table2: str
    column2: str
    reasoning: str = ""


class EntityRelationship(BaseModel):
    source_entity: str
    source_entity_primary_key: str = ""
    target_entity: str
    target_entity_primary_key: str = ""
    relationship_type: str = "association"
    join_condition: str = ""
    reasoning: str = ""


class LinkDefinition(BaseModel):
    link_name: str
    link_table_name: str
    hubs_involved: List[str] = Field(default_factory=list)
    join_keys: List[JoinKeyDetail] = Field(default_factory=list)
    relationships: List[EntityRelationship] = Field(default_factory=list)


class SatelliteDefinition(BaseModel):
    entity_name: str
    satellite_table_name: str
    parent_hub: str
    attributes: List[str] = Field(default_factory=list)
    mapped_source_tables: List[str] = Field(default_factory=list)
    change_tracking: Literal["type1", "type2", "append_only"] = "type2"


class PitDefinition(BaseModel):
    pit_table_name: str
    parent_hub: str
    satellites: List[str] = Field(default_factory=list)


class BridgeDefinition(BaseModel):
    bridge_table_name: str
    source_links: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class DV2Model(BaseModel):
    hubs: List[HubDefinition] = Field(default_factory=list)
    links: List[LinkDefinition] = Field(default_factory=list)
    satellites: List[SatelliteDefinition] = Field(default_factory=list)
    pits: List[PitDefinition] = Field(default_factory=list)
    bridges: List[BridgeDefinition] = Field(default_factory=list)
    hash_key_strategy: HashKeyStrategy = Field(default_factory=HashKeyStrategy)


class ColumnSchema(BaseModel):
    name: str
    data_type: str
    primary_key: bool = False
    nullable: bool = True
    description: Optional[str] = None


class FactTable(BaseModel):
    name: str
    grain_statement: str
    measures: List[FieldDefinition] = Field(default_factory=list)
    foreign_keys: List[str] = Field(default_factory=list)
    degenerate_dimensions: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class DimensionTable(BaseModel):
    name: str
    attributes: List[FieldDefinition] = Field(default_factory=list)
    surrogate_key: Optional[str] = None
    natural_keys: List[str] = Field(default_factory=list)
    slowly_changing_type: Optional[Literal["type1", "type2", "type3", "type6"]] = None
    description: Optional[str] = None


class DimensionalModel(BaseModel):
    """Kimball-style physical IR.

    The ``variant`` field was promoted to a typed choice in D6 so
    downstream emitters can branch structurally (DDL shape, SQL join
    strategy, ``emit_dimensional_variants`` file naming) instead of
    reading a string out of ``source_summary``. Defaults to ``"star"``
    — the most common single-process Kimball shape — so every existing
    caller that didn't set ``variant`` keeps working unchanged.
    """

    facts: List[FactTable] = Field(default_factory=list)
    dimensions: List[DimensionTable] = Field(default_factory=list)
    conformed_dimensions: List[str] = Field(default_factory=list)
    bridges: List[str] = Field(default_factory=list)
    degenerate_dims: List[str] = Field(default_factory=list)
    slowly_changing: dict[str, Literal["type1", "type2", "type3", "type6"]] = Field(
        default_factory=dict
    )
    grain_statement: str = ""
    variant: DimensionalVariant = "star"


def recommend_dimensional_variant(model: "DimensionalModel") -> DimensionalVariant:
    """Pick the variant that best matches the shape of ``model``.

    Deterministic helper the modeler can call when the user didn't
    request a specific flavor:

    * Multiple facts sharing conformed dimensions → **galaxy**.
    * A single fact with dims that declare SCD2 or nested relationships
      → **snowflake** (normalized levels pay off more than denorm).
    * A single fact and small dim count → **star** (default).
    * No dimensions at all → **flat** (fact is already self-contained).

    This is advisory; the modeler's prompt or the user's ``--variant``
    flag override it. Having one canonical rule keeps the default
    behavior reproducible without an LLM call.
    """
    fact_count = len(model.facts)
    dim_count = len(model.dimensions)

    if fact_count >= 2 and model.conformed_dimensions:
        return "galaxy"
    if dim_count == 0 and fact_count <= 1:
        return "flat"
    if fact_count == 1 and any(
        dim.slowly_changing_type in ("type2", "type3", "type6") for dim in model.dimensions
    ):
        return "snowflake"
    return "star"
