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

"""Comprehensive ``TableMetadata`` — Pydantic port of Model AI's 6-category
table-metadata schema.

The schema groups every property the downstream dbt / DDL / governance
tooling needs into six MUST/RECOMMENDED sections so the LLM (or a
deterministic DDL parser) can emit a complete description of a table
in one pass:

- **A. Identification** (MUST) — ``table_id``, ``table_type``,
  ``business_domain``, ``description``.
- **B. Keys & Keys Strategy** (MUST) — business keys, hashing algorithm,
  surrogate key, natural-key flag, referential keys.
- **C. Physical / Storage** (MUST) — file format, partitioning,
  clustering, compression, encryption, storage tier.
- **D. Load Strategy & Frequency** (MUST) — load strategy, pattern,
  frequency, source systems, CDC capability.
- **E. Constraints & Indexes** (MUST / RECOMMENDED) — primary keys,
  unique/not-null constraints, indexes, business rules.
- **F. Lifecycle & Governance** (RECOMMENDED) — retention, PII, owner,
  steward, SLAs, DQ checks, lineage, tags.

Matches the Model AI spec at [`tools/table_metadata.py:70-121`
](../../../../../Model%20AI/model-ai-core-main/packages/modelling/src/modelling/tools/table_metadata.py)
so operators who came from that tooling see identical field names.
This port is a Pydantic ``BaseModel`` (not a dataclass) to keep every
copilot schema validatable via ``model_validate_json`` and emittable
via ``model_dump(mode="json")`` — the standard forge-cli round-trip.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# ----------------------------------------------------------------------
# Category B — Keys & Keys Strategy
# ----------------------------------------------------------------------


class SurrogateKey(BaseModel):
    """Surrogate-key configuration.

    Example::

        SurrogateKey(
            name="customer_sk",
            type="BIGINT",
            generation_rule="identity(1,1)",
        )
    """

    name: str
    type: str
    generation_rule: str


class ReferentialKey(BaseModel):
    """Outgoing reference from this table to another table's column.

    ``cardinality`` is a short human-readable hint such as ``"N:1"`` or
    ``"1:1"`` — free-form today to stay compatible with Model AI output.
    """

    target_table: str
    target_column: str
    cardinality: str


# ----------------------------------------------------------------------
# Category C — Physical / Storage
# ----------------------------------------------------------------------


PartitionType = Literal["date", "range", "hash", "none"]
"""Supported partition strategies. ``"none"`` signals intentional
absence (as opposed to omitting the field, which leaves the choice to
the emitter's default)."""


class Partitioning(BaseModel):
    """Partitioning plan for a physical table.

    ``column`` is required when ``type != "none"``; the emitter fails
    closed if it finds ``type="date"`` without a column. We don't
    enforce that invariant in the schema itself so partial/LLM-drafted
    snippets can round-trip without an immediate validation error —
    callers that need the strictness run the emitter-side validator.
    """

    type: PartitionType
    column: Optional[str] = None
    freq: Optional[str] = None  # e.g. "daily", "monthly"
    retention_days: Optional[int] = None


StorageTier = Literal["hot", "warm", "cold"]
"""Common tiering terms across Snowflake, BigQuery, Databricks, S3, etc."""


# ----------------------------------------------------------------------
# Category E — Constraints & Indexes
# ----------------------------------------------------------------------


class Index(BaseModel):
    """Secondary index definition. ``columns`` preserves order because
    many engines treat ``(a, b)`` and ``(b, a)`` as different indexes."""

    columns: List[str]
    type: str  # "btree" | "hash" | "bitmap" | engine-specific


# ----------------------------------------------------------------------
# Category F — Lifecycle & Governance
# ----------------------------------------------------------------------


class SLA(BaseModel):
    """Service Level Agreement — RTO/RPO bounds for recovery planning."""

    RTO: str  # Recovery Time Objective, e.g. "4h"
    RPO: str  # Recovery Point Objective, e.g. "1h"


class DataQualityCheck(BaseModel):
    """One DQ rule attached to the table (row-count, uniqueness, range…).

    ``threshold`` and ``parameters`` are open-ended to stay compatible
    with every DQ framework we've integrated with (Great Expectations,
    dbt-utils, Soda).
    """

    name: str
    check_type: str
    threshold: Optional[float] = None
    parameters: Optional[Dict[str, Any]] = None


# ----------------------------------------------------------------------
# Top-level schema — six categories, same field names as Model AI
# ----------------------------------------------------------------------


PurgePolicy = Literal["soft", "hard", "none"]
DataClassification = Literal["public", "internal", "confidential", "restricted"]
LoadStrategy = Literal[
    "append_snapshot",
    "append_incremental",
    "merge_upsert",
    "truncate_reload",
    "scd_type_2",
]
LoadPattern = Literal["batch", "streaming", "microbatch", "on_demand"]
LoadFrequency = Literal[
    "continuous",
    "hourly",
    "daily",
    "weekly",
    "monthly",
    "quarterly",
    "ad_hoc",
]


class TableMetadata(BaseModel):
    """Complete table metadata in six categories.

    Instantiate with the MUST fields; every other field has a safe
    default so a stub can grow into a fully-specified artefact without
    re-shaping early callers::

        TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="Conformed customer dimension (SCD2).",
            business_key_columns=["customer_id"],
            business_key_hash=True,
            business_key_hash_algo="md5",
        )
    """

    # ------------------------------------------------------------------
    # A. Identification (MUST)
    # ------------------------------------------------------------------
    table_id: str
    table_type: str  # "hub" | "link" | "satellite" | "dimension" | "fact" | ...
    business_domain: str
    description: str

    # ------------------------------------------------------------------
    # B. Keys & Keys Strategy (MUST)
    # ------------------------------------------------------------------
    business_key_columns: List[str] = Field(default_factory=list)
    business_key_hash: bool = False
    business_key_hash_algo: Optional[str] = None  # "md5" | "sha256" | ...
    surrogate_key: Optional[SurrogateKey] = None
    natural_key_present: bool = False
    referential_keys: List[ReferentialKey] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # C. Physical / Storage (MUST)
    # ------------------------------------------------------------------
    file_format: str = "parquet"  # "parquet" | "orc" | "delta" | "iceberg" | ...
    partitioning: Optional[Partitioning] = None
    clustering_keys: List[str] = Field(default_factory=list)
    compression: str = "snappy"
    encryption: bool = False
    storage_tier: StorageTier = "hot"

    # ------------------------------------------------------------------
    # D. Load Strategy & Frequency (MUST)
    # ------------------------------------------------------------------
    load_strategy: LoadStrategy = "append_snapshot"
    load_pattern: LoadPattern = "batch"
    load_frequency: LoadFrequency = "daily"
    source_systems: List[str] = Field(default_factory=list)
    cdc_capable: bool = False

    # ------------------------------------------------------------------
    # E. Constraints & Indexes (MUST / recommended)
    # ------------------------------------------------------------------
    primary_key: List[str] = Field(default_factory=list)
    unique_constraints: List[List[str]] = Field(default_factory=list)
    not_null_constraints: List[str] = Field(default_factory=list)
    indexes: List[Index] = Field(default_factory=list)
    business_rules: List[str] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # F. Lifecycle & Governance (RECOMMENDED)
    # ------------------------------------------------------------------
    retention_policy_days: Optional[int] = None
    purge_policy: PurgePolicy = "soft"
    pii_flag: bool = False
    pii_columns: List[str] = Field(default_factory=list)
    data_classification: DataClassification = "internal"
    owner: Optional[str] = None
    steward: Optional[str] = None
    sla: Optional[SLA] = None
    data_quality_checks: List[DataQualityCheck] = Field(default_factory=list)
    lineage_ref: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


__all__ = [
    "DataClassification",
    "DataQualityCheck",
    "Index",
    "LoadFrequency",
    "LoadPattern",
    "LoadStrategy",
    "PartitionType",
    "Partitioning",
    "PurgePolicy",
    "ReferentialKey",
    "SLA",
    "StorageTier",
    "SurrogateKey",
    "TableMetadata",
]
