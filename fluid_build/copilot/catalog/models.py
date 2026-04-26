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

"""Pydantic shapes for catalog metadata.

Every catalog adapter normalizes its native API into these shapes so
downstream stages of the forge pipeline (Logical / Builder /
Transformation) can consume catalog metadata without knowing which
catalog produced it.

The shapes are intentionally a SUPERSET of any single catalog — fields
that don't apply to a given catalog default to ``None`` / empty list.
For example, Snowflake doesn't have first-class column-mask metadata
the way Unity does; a Snowflake adapter leaves
:attr:`CatalogColumn.mask_expression` as ``None`` rather than
inventing one.

Sensitivity classifications use a typed enum (:class:`SensitivityTag`)
because downstream policy decisions (Fluid ``agentPolicy.sensitiveData``,
dbt ``meta:`` blocks) MUST distinguish PII from PHI from PCI — silently
collapsing them would defeat the regulatory purpose.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SensitivityTag(str, Enum):
    """Typed sensitivity classifications.

    The set is the union of the major regulatory regimes' top-level
    categories. Catalog-specific finer-grained classifications (e.g.,
    Snowflake's ``IDENTIFIER`` / ``QUASI_IDENTIFIER`` /
    ``SENSITIVE``) are stored in :attr:`CatalogColumn.classifications`
    as raw strings; this enum is the cross-catalog SUPERSET that
    downstream policy code actually branches on.
    """

    PII = "pii"
    PHI = "phi"
    PCI = "pci"
    GDPR = "gdpr"
    HIPAA = "hipaa"
    CONFIDENTIAL = "confidential"
    PUBLIC = "public"


class CatalogScope(BaseModel):
    """Query scope for ``CatalogAdapter.list_tables``.

    Each catalog interprets the ``database`` / ``schema`` / ``catalog``
    triple in its own native way. Snowflake uses (database, schema);
    Unity uses (catalog, schema); BigQuery uses (project, dataset).
    The shared :class:`CatalogScope` lets MCP tools and the CLI pass
    a single typed object regardless of which adapter consumes it.

    ``tables`` is an optional explicit list — when absent, the adapter
    enumerates everything under the (database, schema) pair. When
    present, only those tables are queried (saves a round trip on
    schemas with thousands of tables).

    The Python field is named :attr:`schema_name` rather than ``schema``
    because ``schema`` shadows :meth:`BaseModel.schema` in Pydantic; the
    JSON alias ``schema`` keeps catalog-MCP-tool callers happy. Consumers
    can pass either ``schema=...`` or ``schema_name=...`` thanks to
    ``populate_by_name=True``.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    database: Optional[str] = None
    schema_name: Optional[str] = Field(default=None, alias="schema")
    catalog: Optional[str] = None
    tables: List[str] = Field(default_factory=list)


class CatalogColumn(BaseModel):
    """One column's metadata as understood by the catalog.

    Field selection follows the OSI v0.1.1 ``OSIField`` shape so the
    Logical-stage mapping is mechanical: ``name`` → ``OSIField.name``,
    ``description`` → ``OSIField.expression.description``,
    ``classifications`` → ``OSIField.custom_extensions[]``, etc.

    Three fields are catalog-specific superset:

    * :attr:`mask_expression` — Unity's column mask SQL; Snowflake's
      masking-policy ID; BigQuery's row-access-policy ref. The
      modeler does NOT try to invent the masked column's content.
    * :attr:`partition_key` / :attr:`clustering_key` — feeds the
      TransformationAgent's dbt ``partition_by`` / ``cluster_by``
      configs.
    * :attr:`sensitivity_tags` — typed superset; downstream policy
      keys off this rather than the raw ``classifications`` list.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    data_type: str
    nullable: bool = True
    description: Optional[str] = None
    primary_key: bool = False
    partition_key: bool = False
    clustering_key: bool = False
    classifications: List[str] = Field(default_factory=list)
    sensitivity_tags: List[SensitivityTag] = Field(default_factory=list)
    mask_expression: Optional[str] = None
    business_glossary_terms: List[str] = Field(default_factory=list)
    catalog_specific: Dict[str, Any] = Field(default_factory=dict)


class CatalogForeignKey(BaseModel):
    """One foreign-key declaration from the catalog.

    Catalogs declare FKs explicitly (Snowflake / BigQuery
    INFORMATION_SCHEMA, Unity Catalog table constraints, DataHub /
    DMM relationship entries). When present, the modeler uses these
    DIRECTLY instead of LLM-inferring relationships from column
    naming heuristics — a huge accuracy lift.
    """

    model_config = ConfigDict(frozen=True)

    constraint_name: Optional[str] = None
    from_columns: List[str]
    to_table: str
    to_columns: List[str]


class LineageRef(BaseModel):
    """One step in a catalog's lineage chain.

    ``kind`` distinguishes "this table was built by transforming X"
    (``upstream``) from "this table is consumed by Y" (``downstream``)
    so the BuilderAgent can populate Fluid's ``metadata.lineage.upstream[]``
    correctly.
    """

    model_config = ConfigDict(frozen=True)

    fqn: str
    kind: Literal["upstream", "downstream"]
    transformation_type: Optional[str] = None  # "view", "ctas", "dbt", etc.


class CatalogLineage(BaseModel):
    """Full upstream + downstream lineage for one table FQN.

    Empty lists are valid — not every table has known lineage
    (greenfield raw tables, manually-loaded fixtures). Adapters
    return the empty model rather than ``None`` so consumers don't
    have to defensively check for missing-lineage.
    """

    model_config = ConfigDict(frozen=True)

    upstream: List[LineageRef] = Field(default_factory=list)
    downstream: List[LineageRef] = Field(default_factory=list)


class CatalogTable(BaseModel):
    """One table's full metadata as collected from the catalog.

    The cross-catalog SUPERSET. Every adapter populates as many
    fields as its native API supports; missing fields default to
    ``None`` / empty list. Downstream stages of the forge pipeline
    consume this without caring which catalog it came from.

    Field provenance:

    * ``fqn``, ``database``, ``schema``, ``name`` — universal.
    * ``description``, ``owner``, ``steward`` — Snowflake COMMENT,
      Unity table.comment + owner, BigQuery description + labels,
      DataHub editable properties.
    * ``tags`` — Snowflake OBJECT_TAGS, Unity tags, BigQuery labels,
      Glue table parameters, DataHub tag aspects.
    * ``classifications`` — Snowflake SYSTEM$CLASSIFY, Unity column
      classifications, Dataplex aspect-types, DataHub
      sensitive-data tags.
    * ``foreign_keys`` — INFORMATION_SCHEMA.TABLE_CONSTRAINTS
      (Snowflake / BigQuery), Unity table FK declarations, DataHub
      relationship aspects.
    * ``lineage`` — Snowflake OBJECT_DEPENDENCIES, Unity lineage
      API, Dataplex lineage entries, DataHub upstream/downstream
      aspects, DMM lineage chains.
    * ``partition_keys`` / ``clustering_keys`` — BigQuery
      INFORMATION_SCHEMA.PARTITIONS, Snowflake CLUSTER BY,
      Unity partition info.
    * ``data_quality_score`` / ``freshness_sla`` — Dataplex aspect
      types, DataHub data-quality aspects, Unity certification.
    * ``last_modified`` — table-level audit timestamp. Drives
      cache-key invalidation when the catalog snapshot changes.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    # Universal identity
    fqn: str
    database: Optional[str] = None
    schema_name: Optional[str] = Field(default=None, alias="schema")
    name: str

    # Business metadata
    description: Optional[str] = None
    owner: Optional[str] = None
    steward: Optional[str] = None
    domain: Optional[str] = None
    tags: Dict[str, str] = Field(default_factory=dict)
    classifications: List[str] = Field(default_factory=list)
    sensitivity_tags: List[SensitivityTag] = Field(default_factory=list)
    certification_level: Optional[str] = None  # "certified" | "sandbox" | None

    # Schema
    columns: List[CatalogColumn] = Field(default_factory=list)
    primary_key_columns: List[str] = Field(default_factory=list)
    foreign_keys: List[CatalogForeignKey] = Field(default_factory=list)
    partition_keys: List[str] = Field(default_factory=list)
    clustering_keys: List[str] = Field(default_factory=list)

    # Lineage + quality
    lineage: Optional[CatalogLineage] = None
    glossary_terms: List[str] = Field(default_factory=list)
    data_quality_score: Optional[float] = None
    freshness_sla: Optional[str] = None  # ISO 8601 duration
    quality_rules: List[str] = Field(default_factory=list)

    # Sovereignty / compliance
    data_residency: Optional[str] = None
    compliance_profile: Optional[str] = None  # "gdpr", "hipaa", "pci-dss", etc.

    # Catalog-specific overflow
    last_modified: Optional[datetime] = None
    catalog_specific: Dict[str, Any] = Field(default_factory=dict)


class GlossaryTerm(BaseModel):
    """One business-glossary entry.

    Glossary terms feed OSI's ``ai_context.synonyms`` and
    ``ai_context.examples``. Multiple glossary terms can attach to
    one table or column; ``related_terms`` carries the cross-link
    graph the catalog provides (e.g., DataHub Term → Term
    relationships).
    """

    model_config = ConfigDict(frozen=True)

    term: str
    definition: str
    synonyms: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)
    related_terms: List[str] = Field(default_factory=list)
    domain: Optional[str] = None
    catalog_specific: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "CatalogColumn",
    "CatalogForeignKey",
    "CatalogLineage",
    "CatalogScope",
    "CatalogTable",
    "GlossaryTerm",
    "LineageRef",
    "SensitivityTag",
]
