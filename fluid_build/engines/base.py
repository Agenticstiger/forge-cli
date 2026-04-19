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

"""Transformation engine base class and supporting types.

Every transformation engine (dbt, sql, python, spark, …) subclasses
:class:`TransformationEngine` and registers itself with the engine
registry.  The framework separates **what** transformation to run
(engine) from **where** to run it (provider).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------


class Severity(Enum):
    """Validation issue severity."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """A single validation problem found by :meth:`TransformationEngine.validate`."""

    message: str
    severity: Severity = Severity.ERROR
    field: Optional[str] = None  # e.g. "builds[0].properties.model"

    def __str__(self) -> str:
        prefix = f"[{self.field}] " if self.field else ""
        return f"{prefix}{self.severity.value}: {self.message}"


@dataclass
class TransformationIntent:
    """AI-generated transformation plan passed to :meth:`TransformationEngine.generate`.

    Produced by the copilot LLM during ``fluid forge``.  Engines use this
    to generate full transformation logic rather than skeleton stubs.
    When ``None`` is passed, engines fall back to skeleton generation.
    """

    source_schemas: Dict[str, Dict[str, str]] = field(default_factory=dict)
    """Mapping of source table name → {column_name: type}."""

    target_schema: Dict[str, str] = field(default_factory=dict)
    """Target output schema: {column_name: type}."""

    joins: List[Dict[str, Any]] = field(default_factory=list)
    """Join specifications: [{left, right, keys, type}]."""

    aggregations: List[Dict[str, Any]] = field(default_factory=list)
    """Aggregation rules: [{measures, dimensions, filters}]."""

    filters: List[str] = field(default_factory=list)
    """SQL WHERE clause fragments."""

    stages: List[Dict[str, Any]] = field(default_factory=list)
    """Ordered transformation stages: [{name, sql, layer, depends_on, outputs}]."""

    canonical_model: Optional[str] = None
    """Domain canonical model name if used (e.g. 'tm_forum_sid', 'hl7_fhir')."""

    user_data_model: Optional[Dict[str, Any]] = None
    """Parsed user-supplied data model (guardrails for AI generation)."""

    data_modeling_technique: Optional[str] = None
    """Canonical modeling technique driving the fallback skeleton naming.

    One of ``'data_vault_2'`` or ``'dimensional'`` (or ``None`` when the
    interview was never run).  Populated by
    :func:`fluid_build.cli.forge_copilot_interview.normalize_interview_value`.
    When the LLM ships real SQL in ``additional_files`` this field is
    informational; when it doesn't, the engine uses it to pick between
    hub/link/satellite skeletons and stg/dim/fct skeletons.
    """


# ---------------------------------------------------------------------------
# GenerationResult type alias
# ---------------------------------------------------------------------------

#: Return type of :meth:`TransformationEngine.generate`.
#: Maps relative file paths to their string content.
#: Example: ``{"dbt_project.yml": "...", "models/staging/stg_orders.sql": "..."}``
GenerationResult = Dict[str, str]


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class TransformationEngine(ABC):
    """Abstract base for all transformation engines.

    Subclasses **must** implement :meth:`generate` and :meth:`validate`.

    The engine always receives the *bundled* (``$ref``-resolved) contract
    dict — fragment resolution is the caller's responsibility.

    Future methods (``plan``, ``execute``) are reserved but not abstract,
    so existing engines won't break when they're added.
    """

    #: Engine identifier (e.g. ``"dbt"``, ``"sql"``).
    name: str = ""

    #: Build patterns this engine supports.
    supported_patterns: Sequence[str] = ()

    #: Platforms this engine works with.
    #: ``None`` or empty means platform-agnostic (works everywhere).
    #: Set to e.g. ``("gcp",)`` for GCP-only engines like Dataform.
    supported_platforms: Optional[Sequence[str]] = None

    @abstractmethod
    def generate(
        self,
        contract: Dict[str, Any],
        build: Dict[str, Any],
        *,
        build_index: int = 0,
        schema_context: Optional[Dict[str, Any]] = None,
        transformation_intent: Optional[TransformationIntent] = None,
        workspace_root: Optional[Path] = None,
    ) -> GenerationResult:
        """Generate engine artifacts from contract + optional AI context.

        Parameters
        ----------
        contract:
            The full FLUID contract (bundled, ``$ref``-resolved).
        build:
            The specific ``builds[]`` entry to generate for.
        build_index:
            Index of the build in the contract's ``builds`` array.
        schema_context:
            Discovered sample schemas from the discovery phase.
        transformation_intent:
            AI-generated transformation plan.  When ``None``, the engine
            should produce skeleton files with TODO placeholders.
        workspace_root:
            Directory used as the anchor when the engine needs to locate
            upstream artifacts (e.g. other ``contract.fluid.yaml`` files
            referenced by ``consumes[]``).  Engines that do not consume
            upstream metadata can ignore it.

        Returns
        -------
        Dict mapping relative file paths to their string content.
        """

    @abstractmethod
    def validate(
        self,
        contract: Dict[str, Any],
        build: Dict[str, Any],
    ) -> List[ValidationIssue]:
        """Validate that *build* is valid for this engine.

        Returns an empty list when valid.
        """

    # ------------------------------------------------------------------
    # Future hooks (reserved, not abstract)
    # ------------------------------------------------------------------

    # def plan(self, contract, build, provider_context) -> List[Action]:
    #     """Plan execution actions for a provider."""
    #     raise NotImplementedError
    #
    # def execute(self, actions, runtime_context) -> ApplyResult:
    #     """Execute planned actions."""
    #     raise NotImplementedError
