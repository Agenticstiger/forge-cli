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

Tracks the Apache Ossie core-spec
(https://github.com/apache/ossie/blob/main/core-spec/spec.md) — the
continuation of the OSI v0.1.1 spec this module originally ported.
Relative to v0.1.1 the upstream spec added the ``MAQL`` and ``BIGQUERY``
dialects, made ``vendor_name`` a free-form string (the finite vendor
vocabulary is now advisory), allowed ``ai_context`` to be a plain string,
and introduced a root document wrapper (``{version, semantic_model: []}``).

Two roles, one module:

* **Internal IR** (``OSISemanticModel`` and children) — deliberately
  *more tolerant* than the interchange schema (empty ``datasets`` for
  LLM-repair stubs, optional ``source``/``expression``) and *richer*
  (``OSIField.data_type``, ``OSIDimension.grain``,
  ``OSIRelationship.description`` are fluid-only enrichments consumed by
  the DDL/model-doc/contract emitters).
* **Interchange document** (``OSIDocument``) — the spec-conformant root
  wrapper. Strict emission (required-key guarantees, relocation of the
  fluid-only fields into ``custom_extensions``) is handled by
  :mod:`fluid_build.forge_datamodel.emit.osi_sidecar`, which is the only
  place allowed to serialize an OSI document for external consumers.

Enum fields use ``Literal`` so Pydantic rejects off-spec values at
validate time — the spec's ``dialect`` vocabulary is finite and
downstream integrations (dbt Core's native OSI reader, the upstream
Snowflake Cortex / Salesforce / GoodData converters) rely on those exact
strings.
"""

from __future__ import annotations

from typing import Annotated, Any, List, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# Exact spec vocabulary (Apache Ossie core-spec §Enumerations). Exported so
# the modeler + tests can share one source of truth — changing it here is
# the only edit needed to track an upstream spec revision.
OSIDialect = Literal["ANSI_SQL", "SNOWFLAKE", "MDX", "TABLEAU", "DATABRICKS", "MAQL", "BIGQUERY"]

# The spec version stamped into emitted OSI documents. 0.1.1 is the only
# *released* spec revision (upstream main is the mutable 0.2.0.dev0 draft)
# and the only version dbt Core's native OSI reader accepts today. Single
# chokepoint: bump here when upstream cuts 0.2.0 and consumers catch up.
OSI_SPEC_VERSION = "0.1.1"

# Well-known ``vendor_name`` values from the spec's advisory table. The
# field itself is free-form ("any vendor or organization" may define
# extensions) — this tuple exists for documentation and tests, NOT for
# validation.
OSI_WELL_KNOWN_VENDORS: tuple[str, ...] = (
    "COMMON",
    "SNOWFLAKE",
    "SALESFORCE",
    "DBT",
    "DATABRICKS",
    "GOODDATA",
    "HONEYDEW",
)

# Vendor slug for fluid's own custom extensions. The sidecar emitter
# relocates the internal-IR-only fields (``data_type``, ``grain``,
# ``relationship.description``) into ``custom_extensions`` entries under
# this vendor so emitted documents stay strict-schema-valid
# (``additionalProperties: false`` upstream) without losing information.
FLUID_VENDOR_NAME = "FLUID"

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
    "MAQL",
    "BIGQUERY",
)


def osi_dialect_from_source_type(source_type: Optional[str]) -> OSIDialect:
    """Normalize a forge-cli ``--source-type`` value to an OSI spec dialect.

    The ``from-ddl`` entry point accepts dialect hints (``postgres``,
    ``mysql``, ``bigquery``, ``oracle``, ``snowflake``, ``databricks``)
    to help sqlglot parse the incoming DDL. OSI's ``dialect`` enum is
    narrower — it only enumerates the flavours where SQL expressions
    need vendor-specific syntax. Expressions emitted by the modeler are
    always plain column references (e.g. ``customer_id``), so any
    source_type without an exact OSI dialect maps safely to ANSI_SQL
    without loss of meaning.
    """
    if not source_type:
        return "ANSI_SQL"
    normalized = source_type.strip().upper()
    if normalized == "SNOWFLAKE":
        return "SNOWFLAKE"
    if normalized == "DATABRICKS":
        return "DATABRICKS"
    if normalized == "BIGQUERY":
        return "BIGQUERY"
    # POSTGRES, ORACLE, MYSQL, REDSHIFT, DUCKDB, etc. — the expression we
    # emit is ANSI-compatible, so the OSI-correct label is ANSI_SQL.
    # MDX, TABLEAU, and MAQL are never produced by the from-ddl path.
    return "ANSI_SQL"


class OSIAIContext(BaseModel):
    # Spec: the structured form has additionalProperties: true — preserve
    # unknown keys instead of silently dropping them.
    model_config = ConfigDict(extra="allow")

    instructions: str = ""
    synonyms: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)


def _coerce_ai_context(value: Any) -> Any:
    """Accept the spec's plain-string ``ai_context`` form.

    The spec allows ``ai_context`` to be either a string or a structured
    object. Internal consumers all address the structured attributes
    (``.instructions`` / ``.synonyms``), so the string form canonicalizes
    into ``instructions`` — the closest spec-recommended slot for
    free-text guidance.
    """
    if isinstance(value, str):
        return {"instructions": value}
    return value


# Field alias applying the string→object coercion wherever ai_context is
# accepted. Keeps every internal accessor object-safe while remaining
# input-compatible with both spec forms.
OSIAIContextValue = Annotated[OSIAIContext, BeforeValidator(_coerce_ai_context)]


class OSIExpressionDialect(BaseModel):
    dialect: OSIDialect
    expression: str


class OSIExpression(BaseModel):
    dialects: List[OSIExpressionDialect] = Field(default_factory=list)


class OSIDimension(BaseModel):
    is_time: bool = False
    # Internal IR enrichment — not part of the interchange spec (which
    # defines only ``is_time``). The sidecar emitter relocates it into a
    # field-level FLUID custom_extension; the contract emitter maps it to
    # ``typeParams.timeGranularity``.
    grain: Optional[str] = None


class OSICustomExtension(BaseModel):
    # Free-form per the spec: "any vendor or organization" may define
    # extensions. See OSI_WELL_KNOWN_VENDORS for the advisory vocabulary.
    vendor_name: str
    data: str


class OSIField(BaseModel):
    name: str
    description: Optional[str] = None
    label: Optional[str] = None
    # Internal IR enrichment — not part of the interchange spec. Consumed
    # by the DDL emitter and the dialect back-fill; relocated into a FLUID
    # custom_extension at sidecar emit.
    data_type: Optional[str] = None
    expression: Optional[OSIExpression] = None
    dimension: Optional[OSIDimension] = None
    ai_context: Optional[OSIAIContextValue] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSIDataset(BaseModel):
    name: str
    # Required by the interchange spec; optional in the IR because LLM
    # drafts arrive incrementally. The sidecar emitter defaults a missing
    # source to the dataset name.
    source: Optional[str] = None
    description: Optional[str] = None
    primary_key: List[str] = Field(default_factory=list)
    unique_keys: List[List[str]] = Field(default_factory=list)
    fields: List[OSIField] = Field(default_factory=list)
    ai_context: Optional[OSIAIContextValue] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSIRelationship(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    from_: str = Field(alias="from")
    to: str
    from_columns: List[str] = Field(default_factory=list)
    to_columns: List[str] = Field(default_factory=list)
    # Internal IR enrichment — the interchange spec's Relationship has no
    # description; relocated into a FLUID custom_extension at sidecar emit.
    description: Optional[str] = None
    ai_context: Optional[OSIAIContextValue] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSIMetric(BaseModel):
    name: str
    expression: OSIExpression
    description: Optional[str] = None
    ai_context: Optional[OSIAIContextValue] = None
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSISemanticModel(BaseModel):
    name: str
    description: Optional[str] = None
    ai_context: OSIAIContextValue = Field(default_factory=OSIAIContext)
    datasets: List[OSIDataset] = Field(default_factory=list)
    relationships: List[OSIRelationship] = Field(default_factory=list)
    metrics: List[OSIMetric] = Field(default_factory=list)
    custom_extensions: List[OSICustomExtension] = Field(default_factory=list)


class OSIDocument(BaseModel):
    """Root Ossie interchange document: ``{version, semantic_model: []}``.

    The spec's root wrapper — a single document may carry multiple
    semantic models. Fluid emits one model per document today; build
    instances via
    :func:`fluid_build.forge_datamodel.emit.osi_sidecar.build_osi_document`
    so the IR-only fields are relocated before serialization.
    """

    version: str = OSI_SPEC_VERSION
    semantic_model: List[OSISemanticModel] = Field(default_factory=list)
