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

"""Open Semantic Interchange schemas used inside forged contracts.

Field-for-field port of the OSI v0.1.1 core-spec
(https://github.com/open-semantic-interchange/OSI/blob/main/core-spec/spec.md).
Enum fields use ``Literal`` so Pydantic rejects off-spec values at validate
time — the spec gives finite vocabularies for ``dialect`` and
``vendor_name`` and downstream integrations (dbt, Snowflake Cortex,
Databricks Unity Catalog) rely on those exact strings.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Exact spec vocabularies. Exported so the modeler + tests can share one
# source of truth — changing them here is the only edit needed to track
# an OSI spec revision.
OSIDialect = Literal["ANSI_SQL", "SNOWFLAKE", "MDX", "TABLEAU", "DATABRICKS"]
OSIVendorName = Literal["COMMON", "SNOWFLAKE", "SALESFORCE", "DBT", "DATABRICKS"]

# Tuple of every OSI-validated dialect name. Tools that produce
# ``OSIExpressionDialect`` rows (e.g. the deterministic
# :class:`fluid_build.forge_datamodel.sql.DialectMapper`) MUST
# filter their output through this set or risk a Pydantic
# validation error when the row is written back into OSI.
#
# Kept synced with :data:`OSIDialect` by hand because Python's
# typing module exposes Literal arguments only via internal
# helpers (``get_args``) — leaving the duplication explicit makes
# spec-tracking edits more obvious in code review.
OSI_SUPPORTED_DIALECTS: tuple[str, ...] = (
    "ANSI_SQL",
    "SNOWFLAKE",
    "MDX",
    "TABLEAU",
    "DATABRICKS",
)


def osi_dialect_from_source_type(source_type: Optional[str]) -> OSIDialect:
    """Normalize a forge-cli ``--source-type`` value to an OSI spec dialect.

    The ``from-ddl`` entry point accepts dialect hints (``postgres``,
    ``mysql``, ``bigquery``, ``oracle``, ``snowflake``, ``databricks``)
    to help sqlglot parse the incoming DDL. OSI's ``dialect`` enum is
    narrower — it only enumerates the flavours where SQL expressions
    need vendor-specific syntax. Expressions emitted by the modeler are
    always plain column references (e.g. ``customer_id``), so any
    source_type that isn't explicitly SNOWFLAKE or DATABRICKS maps
    safely to ANSI_SQL without loss of meaning.
    """
    if not source_type:
        return "ANSI_SQL"
    normalized = source_type.strip().upper()
    if normalized == "SNOWFLAKE":
        return "SNOWFLAKE"
    if normalized == "DATABRICKS":
        return "DATABRICKS"
    # BIGQUERY, POSTGRES, ORACLE, MYSQL, REDSHIFT, DUCKDB, etc. — the
    # expression we emit is ANSI-compatible, so the OSI-correct label is
    # ANSI_SQL. MDX and TABLEAU are never produced by the from-ddl path.
    return "ANSI_SQL"


class OSIAIContext(BaseModel):
    instructions: str = ""
    synonyms: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)


class OSIExpressionDialect(BaseModel):
    dialect: OSIDialect
    expression: str


class OSIExpression(BaseModel):
    dialects: List[OSIExpressionDialect] = Field(default_factory=list)


class OSIDimension(BaseModel):
    is_time: bool = False
    grain: Optional[str] = None


class OSICustomExtension(BaseModel):
    vendor_name: OSIVendorName
    data: str


class OSIField(BaseModel):
    name: str
    description: Optional[str] = None
    label: Optional[str] = None
    data_type: Optional[str] = None
    expression: Optional[OSIExpression] = None
    dimension: Optional[OSIDimension] = None
    ai_context: Optional[OSIAIContext] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSIDataset(BaseModel):
    name: str
    source: Optional[str] = None
    description: Optional[str] = None
    primary_key: List[str] = Field(default_factory=list)
    unique_keys: List[List[str]] = Field(default_factory=list)
    fields: List[OSIField] = Field(default_factory=list)
    ai_context: Optional[OSIAIContext] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSIRelationship(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    from_: str = Field(alias="from")
    to: str
    from_columns: List[str] = Field(default_factory=list)
    to_columns: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    ai_context: Optional[OSIAIContext] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSIMetric(BaseModel):
    name: str
    expression: OSIExpression
    description: Optional[str] = None
    ai_context: Optional[OSIAIContext] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSISemanticModel(BaseModel):
    name: str
    description: Optional[str] = None
    ai_context: OSIAIContext = Field(default_factory=OSIAIContext)
    datasets: List[OSIDataset] = Field(default_factory=list)
    relationships: List[OSIRelationship] = Field(default_factory=list)
    metrics: List[OSIMetric] = Field(default_factory=list)
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)
