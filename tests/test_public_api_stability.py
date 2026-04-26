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

"""Lock in the v1.0 public API surface — frozen-snapshot regression test.

Closes V1.3.6 (public-API stability snapshot from the world-class plan).

Why this test exists
--------------------

Until v1.0 ships publicly we are free to rename anything. Once it
ships, any rename / removal of a symbol that's documented as part of
the v1.0 public API silently breaks every user's import. This test
captures the v1.0 surface as a frozen list of ``(module_path,
symbol)`` pairs and asserts every one of them resolves at runtime.

* **Adding a new symbol to a covered module is fine** — the test
  doesn't check for unknown extras.
* **Removing a v1.0 symbol fails the test loudly** — forces an
  intentional deprecation cycle (or a bump to v2 with the breaking
  change called out in the changelog).
* **Renaming a v1.0 symbol fails the test loudly** — same reason.

Scope
-----

The snapshot covers the **documented stable surface**:

* Agents — `coordinator`, `base`, `errors`, and the five-agent split.
* Stage Pydantic schemas — anything a user constructs to drive a
  forge or that the forge returns.
* Store — `Store` ABC, every concrete backend, and the `factory`
  helper.
* Industry pack — the `IndustryPack` family and its compiler.
* DV2 helpers — hash-keys + naming conventions.
* Emit helpers — variant + Fluid contract + DDL emit entry points.
* Banner / quiet — `print_v2_banner`, `banner_enabled`,
  `next_milestone`, `compact_next_line`.
* Typed exceptions.
* `forge_data_model` CLI dispatch helper.

The snapshot is a *list* in this file (not a JSON sidecar) so a PR
that breaks the surface AND the snapshot in one go shows up clearly
in code review.
"""

from __future__ import annotations

import importlib
from typing import List, Tuple

import pytest

# ----------------------------------------------------------------------
# v1.0 public API snapshot
# ----------------------------------------------------------------------

# Each entry is (module_dotted_path, symbol_name).
V1_PUBLIC_API: List[Tuple[str, str]] = [
    # ---- Agents ------------------------------------------------------
    ("fluid_build.copilot.agents.coordinator", "StageCoordinator"),
    ("fluid_build.copilot.agents.base", "BaseStageAgent"),
    ("fluid_build.copilot.agents.base", "StageSession"),
    ("fluid_build.copilot.agents.base", "retry_with_backoff"),
    # Sprint #9 — ``ModelerAgent`` and ``ConceptualAgent`` were
    # composed inside ``LogicalAgent`` from V1.5 onward and the
    # coordinator never invokes them directly. They remain
    # importable for v1.3 orchestrators that pre-date the split,
    # but they're INTENTIONALLY NOT pinned in the public API —
    # the agentic surface that ships in v1.5+ is just
    # :class:`LogicalAgent` (the user-facing composition wrapper).
    # Removing the pins lets us drop these classes in v2 without
    # a deprecation cycle.
    ("fluid_build.copilot.agents.logical_agent", "LogicalAgent"),
    ("fluid_build.copilot.agents.builder_agent", "BuilderAgent"),
    ("fluid_build.copilot.agents.transformation_agent", "TransformationAgent"),
    ("fluid_build.copilot.agents.readme_agent", "ReadmeAgent"),
    ("fluid_build.copilot.agents.validator_agent", "ValidatorAgent"),
    # V1.5 Sprint E — pre-emit conformance lint.
    ("fluid_build.copilot.agents.conformance_agent", "ConformanceAgent"),
    # Missing #2 — proactive heuristic critic.
    ("fluid_build.copilot.agents.critic_agent", "CriticAgent"),
    # Missing #1 — inter-agent scratchpad.
    ("fluid_build.copilot.scratchpad", "Scratchpad"),
    ("fluid_build.copilot.scratchpad", "CriticFinding"),
    ("fluid_build.copilot.scratchpad", "RetrievalResult"),
    ("fluid_build.copilot.scratchpad", "StageFeedback"),
    # E10 — multi-turn cooperation loop.
    ("fluid_build.copilot.agents.cooperation_loop", "run_with_critic_loop"),
    ("fluid_build.copilot.agents.cooperation_loop", "CooperationOutcome"),
    # E11 + E12 — confidence + provenance.
    ("fluid_build.copilot.confidence", "Annotation"),
    ("fluid_build.copilot.confidence", "AnnotationLog"),
    ("fluid_build.copilot.confidence", "ClaimProvenance"),
    ("fluid_build.copilot.confidence", "Confidence"),
    ("fluid_build.copilot.confidence", "confidence_level"),
    # E13 — plan-then-execute.
    ("fluid_build.copilot.planning", "PlanStep"),
    ("fluid_build.copilot.planning", "StagePlan"),
    ("fluid_build.copilot.planning", "record_plan"),
    ("fluid_build.copilot.planning", "get_plan"),
    # E14 — tool-use.
    ("fluid_build.copilot.agent_tools", "Tool"),
    ("fluid_build.copilot.agent_tools", "ToolRegistry"),
    ("fluid_build.copilot.agent_tools", "ToolInvocation"),
    ("fluid_build.copilot.agent_tools", "build_default_tool_registry"),
    # E15 — streaming.
    ("fluid_build.copilot.streaming", "StreamingCall"),
    ("fluid_build.copilot.streaming", "NullStreamHandler"),
    ("fluid_build.copilot.streaming", "stream_to_console"),
    # E16 — continuous learning.
    ("fluid_build.copilot.learning", "OperatorEdit"),
    ("fluid_build.copilot.learning", "compute_edits"),
    ("fluid_build.copilot.learning", "record_operator_edits"),
    ("fluid_build.copilot.learning", "fetch_recent_edits"),
    # E18 — projections + budgets.
    ("fluid_build.copilot.projections", "CostProjection"),
    ("fluid_build.copilot.projections", "StageBudget"),
    ("fluid_build.copilot.projections", "StageBudgetExceeded"),
    ("fluid_build.copilot.projections", "project_run_cost"),
    ("fluid_build.copilot.projections", "recent_run_costs"),
    # Sprint #6 — cost ceiling enforcement.
    ("fluid_build.copilot.cost", "CostLimitExceeded"),
    ("fluid_build.copilot.cost", "check_cost_ceiling"),
    ("fluid_build.copilot.cost", "set_pre_emit_conformance_summary"),
    ("fluid_build.copilot.cost", "get_pre_emit_conformance_summary"),
    ("fluid_build.copilot.agents.conformance_agent", "ConformanceReport"),
    ("fluid_build.copilot.agents.conformance_agent", "SUPPORTED_STANDARDS"),
    # V1.5 Sprint E / Gap 10 — deterministic multi-dialect type mapper.
    ("fluid_build.forge_datamodel.sql", "DialectMapper"),
    ("fluid_build.forge_datamodel.sql", "DEFAULT_DIALECTS"),
    ("fluid_build.forge_datamodel.sql", "MappingResult"),
    ("fluid_build.forge_datamodel.sql", "ValidationReport"),
    # ---- Typed exceptions -------------------------------------------
    ("fluid_build.copilot.agents.errors", "FluidGenerationError"),
    ("fluid_build.copilot.agents.errors", "DDLGenerationError"),
    ("fluid_build.copilot.agents.errors", "AgentExecutionError"),
    # ---- Stage Pydantic schemas (top-level) -------------------------
    # Lean v1 deliberately collapsed ``ScaffoldDecision`` into
    # ``ModelerAgent`` and split ``LogicalDraft`` from ``PhysicalDraft``
    # — both decisions are part of the stable v1.0 API. The list
    # below mirrors what actually ships in
    # ``fluid_build/copilot/schemas/stage_outputs.py``; the absence of
    # ``ScaffoldDecision`` is intentional (Lean v1 §2 "Collapse 5 agents
    # → 2 agents").
    ("fluid_build.copilot.schemas.stage_outputs", "ConceptualDraft"),
    ("fluid_build.copilot.schemas.stage_outputs", "ConceptualEntity"),
    ("fluid_build.copilot.schemas.stage_outputs", "ConceptualRelationship"),
    ("fluid_build.copilot.schemas.stage_outputs", "LogicalDraft"),
    ("fluid_build.copilot.schemas.stage_outputs", "PhysicalDraft"),
    ("fluid_build.copilot.schemas.stage_outputs", "TransformPlan"),
    ("fluid_build.copilot.schemas.stage_outputs", "BuildSpec"),
    ("fluid_build.copilot.schemas.stage_outputs", "ReadmeDraft"),
    ("fluid_build.copilot.schemas.stage_outputs", "ValidationReport"),
    ("fluid_build.copilot.schemas.stage_outputs", "ValidationFinding"),
    ("fluid_build.copilot.schemas.stage_outputs", "BusinessIntent"),
    ("fluid_build.copilot.schemas.stage_outputs", "StructuredOutputModel"),
    ("fluid_build.copilot.schemas.stage_outputs", "TechniqueLiteral"),
    # ---- Data-model schemas -----------------------------------------
    ("fluid_build.copilot.schemas.data_model", "DV2Model"),
    ("fluid_build.copilot.schemas.data_model", "DimensionalModel"),
    ("fluid_build.copilot.schemas.data_model", "DimensionalVariant"),
    ("fluid_build.copilot.schemas.data_model", "DIMENSIONAL_VARIANTS"),
    ("fluid_build.copilot.schemas.data_model", "recommend_dimensional_variant"),
    ("fluid_build.copilot.schemas.data_model", "HubDefinition"),
    ("fluid_build.copilot.schemas.data_model", "LinkDefinition"),
    ("fluid_build.copilot.schemas.data_model", "SatelliteDefinition"),
    ("fluid_build.copilot.schemas.data_model", "PitDefinition"),
    ("fluid_build.copilot.schemas.data_model", "BridgeDefinition"),
    ("fluid_build.copilot.schemas.data_model", "FactTable"),
    ("fluid_build.copilot.schemas.data_model", "DimensionTable"),
    ("fluid_build.copilot.schemas.data_model", "HashKeyStrategy"),
    # ---- OSI semantics ----------------------------------------------
    ("fluid_build.copilot.schemas.osi", "OSISemanticModel"),
    ("fluid_build.copilot.schemas.osi", "OSIAIContext"),
    ("fluid_build.copilot.schemas.osi", "OSI_SUPPORTED_DIALECTS"),
    # V1.5+ unified config (Mediocre #5).
    ("fluid_build.copilot.unified_config", "UnifiedConfig"),
    ("fluid_build.copilot.unified_config", "load_unified_config"),
    ("fluid_build.copilot.unified_config", "migrate_legacy_to_unified"),
    ("fluid_build.copilot.unified_config", "unified_config_path"),
    # V1.5+ event bus (Mediocre #4 / Missing #5+#6).
    ("fluid_build.copilot.events", "Event"),
    ("fluid_build.copilot.events", "EventBus"),
    ("fluid_build.copilot.events", "get_event_bus"),
    ("fluid_build.copilot.events", "reset_event_bus"),
    ("fluid_build.copilot.cost", "AgentCostRow"),
    # ---- Store ABC + backends ---------------------------------------
    ("fluid_build.copilot.store.base", "Store"),
    ("fluid_build.copilot.store.backends.null", "NullBackend"),
    ("fluid_build.copilot.store.backends.file", "FileBackend"),
    ("fluid_build.copilot.store.backends.sqlite", "SqliteBackend"),
    ("fluid_build.copilot.store.backends.postgres", "PostgresBackend"),
    ("fluid_build.copilot.store.backends.vector", "VectorBackend"),
    ("fluid_build.copilot.store.factory", "resolve_store"),
    ("fluid_build.copilot.store.keys", "generate_cache_key"),
    ("fluid_build.copilot.store.semantic_writer", "auto_semantic_write_enabled"),
    ("fluid_build.copilot.store.semantic_writer", "write_semantic_record"),
    # ---- Industry packs ---------------------------------------------
    ("fluid_build.copilot.industry", "IndustryPack"),
    ("fluid_build.copilot.industry", "IndustryPackCompiler"),
    ("fluid_build.copilot.industry", "ComplianceProfile"),
    ("fluid_build.copilot.industry", "CanonicalModel"),
    ("fluid_build.copilot.industry", "IndustryDomain"),
    # ---- DV2 helpers ------------------------------------------------
    ("fluid_build.forge_datamodel.dv2", "compute_hash_key"),
    ("fluid_build.forge_datamodel.dv2", "compute_hash_diff"),
    ("fluid_build.forge_datamodel.dv2", "hub_name"),
    ("fluid_build.forge_datamodel.dv2", "link_name"),
    ("fluid_build.forge_datamodel.dv2", "satellite_name"),
    ("fluid_build.forge_datamodel.dv2", "pit_name"),
    ("fluid_build.forge_datamodel.dv2", "bridge_name"),
    # ---- Emit -------------------------------------------------------
    ("fluid_build.forge_datamodel.emit.variants", "emit_dimensional_variants"),
    # ---- DDL parse + profile ----------------------------------------
    ("fluid_build.forge_datamodel.from_ddl.parser", "ColumnDefinition"),
    ("fluid_build.forge_datamodel.from_ddl.parser", "TableDefinition"),
    ("fluid_build.forge_datamodel.from_ddl.profiler", "ColumnStats"),
    ("fluid_build.forge_datamodel.from_ddl.profiler", "TableProfile"),
    ("fluid_build.forge_datamodel.from_ddl.profiler", "sample_columnar_file"),
    ("fluid_build.forge_datamodel.from_ddl.profiler", "sample_directory"),
    ("fluid_build.forge_datamodel.from_ddl.profiler", "merge_profile_into_tables"),
    # ---- Banner / quiet ---------------------------------------------
    ("fluid_build.cli.forge_banner", "print_v2_banner"),
    ("fluid_build.cli.forge_banner", "banner_enabled"),
    ("fluid_build.cli.forge_banner", "next_milestone"),
    ("fluid_build.cli.forge_banner", "compact_next_line"),
    ("fluid_build.cli.forge_banner", "load_milestones"),
    # ---- Forge data-model CLI surface (top-level dispatch) ----------
    ("fluid_build.cli.forge_data_model", "register_forge_subcommand"),
    ("fluid_build.cli.forge_data_model", "run_from_intent_command"),
    ("fluid_build.cli.forge_data_model", "run_from_ddl_command"),
    ("fluid_build.cli.forge_data_model", "run_validate_command"),
    ("fluid_build.cli.forge_data_model", "run_diff_command"),
    # ---- V1.5 catalog ABC + Pydantic shapes -------------------------
    # The catalog stack is the source-side complement to publish
    # adapters; every entry here is documented in the user-facing
    # docs/cli/catalogs/ pages and routed through the public
    # ``fluid forge data-model from-source`` and MCP
    # ``forge_from_source`` surfaces. Removing or renaming any of
    # them silently breaks downstream `from x import y` imports.
    ("fluid_build.copilot.catalog", "CatalogAdapter"),
    ("fluid_build.copilot.catalog", "CatalogTable"),
    ("fluid_build.copilot.catalog", "CatalogColumn"),
    ("fluid_build.copilot.catalog", "CatalogForeignKey"),
    ("fluid_build.copilot.catalog", "CatalogLineage"),
    ("fluid_build.copilot.catalog", "CatalogScope"),
    ("fluid_build.copilot.catalog", "GlossaryTerm"),
    ("fluid_build.copilot.catalog", "LineageRef"),
    ("fluid_build.copilot.catalog", "SensitivityTag"),
    ("fluid_build.copilot.catalog", "CatalogConfigError"),
    ("fluid_build.copilot.catalog", "CatalogConnectionError"),
    ("fluid_build.copilot.catalog", "CatalogPermissionError"),
    ("fluid_build.copilot.catalog", "CredentialResolver"),
    ("fluid_build.copilot.catalog", "CredentialNotFoundError"),
    # Per-catalog credential classes — each is the typed envelope
    # users construct via ``adapter.from_resolver(..., inline_credentials=...)``
    # or ``fluid ai setup --source NAME``.
    ("fluid_build.copilot.catalog", "SnowflakeCredentials"),
    ("fluid_build.copilot.catalog", "UnityCredentials"),
    ("fluid_build.copilot.catalog", "BigQueryCredentials"),
    ("fluid_build.copilot.catalog", "DataplexCredentials"),
    ("fluid_build.copilot.catalog", "GlueCredentials"),
    ("fluid_build.copilot.catalog", "DataHubCredentials"),
    ("fluid_build.copilot.catalog", "DataMeshManagerCredentials"),
    # ---- V1.5 catalog adapters --------------------------------------
    # Concrete adapter classes — public so contributors can extend
    # the registry (e.g., subclass to override one method) and so
    # downstream tooling can detect adapter capabilities at runtime.
    ("fluid_build.copilot.catalog.snowflake", "SnowflakeCatalogAdapter"),
    ("fluid_build.copilot.catalog.unity", "UnityCatalogAdapter"),
    ("fluid_build.copilot.catalog.bigquery", "BigQueryCatalogAdapter"),
    ("fluid_build.copilot.catalog.dataplex", "DataplexCatalogAdapter"),
    ("fluid_build.copilot.catalog.glue", "GlueCatalogAdapter"),
    ("fluid_build.copilot.catalog.datahub", "DataHubCatalogAdapter"),
    (
        "fluid_build.copilot.catalog.datamesh_manager",
        "DataMeshManagerCatalogAdapter",
    ),
    # ---- V1.5 catalog → industry auto-detection ---------------------
    # The mapping table is consumed by ``run_from_source_command`` to
    # auto-pick an industry pack from catalog domain tags. Pinned
    # because external scripts may inspect it to predict the chosen
    # industry before the actual forge runs.
    ("fluid_build.copilot.industry.compiler", "INDUSTRY_DOMAIN_HINTS"),
    ("fluid_build.copilot.industry.compiler", "match_industry_from_domain"),
    (
        "fluid_build.copilot.industry.compiler",
        "match_industry_from_catalog_tags",
    ),
    (
        "fluid_build.copilot.industry.compiler",
        "detect_industry_from_catalog_tables",
    ),
    # ---- V1.5 from-catalog pipeline + LogicalAgent entry ------------
    (
        "fluid_build.forge_datamodel.from_catalog.pipeline",
        "run_from_catalog",
    ),
    (
        "fluid_build.forge_datamodel.from_catalog.pipeline",
        "CatalogPipelineResult",
    ),
    # ``from_catalog`` is the public LogicalAgent classmethod that
    # turns a CatalogScope into a LogicalDraft — pinned because
    # custom orchestrators may wrap it directly.
    ("fluid_build.cli.forge_data_model", "run_from_source_command"),
    ("fluid_build.cli.forge_data_model", "run_learn_command"),
]


@pytest.mark.parametrize(
    "module_path,symbol",
    V1_PUBLIC_API,
    ids=[f"{m}::{s}" for m, s in V1_PUBLIC_API],
)
def test_v1_public_symbol_resolves(module_path: str, symbol: str) -> None:
    """Each name in the v1.0 public API snapshot must resolve.

    Failure here means a refactor renamed or removed a symbol that
    users can ``from <module> import <symbol>``. Add a deprecated
    alias or bump to v2 — never silently break the contract.
    """
    module = importlib.import_module(module_path)
    assert hasattr(module, symbol), (
        f"v1.0 public symbol missing: {module_path}.{symbol} — "
        "removing/renaming a v1.0-public symbol requires a deprecation "
        "cycle (alias retained for one minor release) or a v2 bump."
    )


def test_snapshot_is_non_empty():
    """Sanity guard: an accidentally-empty snapshot would silently
    pass the parametrized test below. Pin the floor."""
    assert len(V1_PUBLIC_API) >= 50


def test_snapshot_has_no_duplicates():
    """Duplicate ``(module, symbol)`` entries waste cycles and obscure
    the real surface size. A future PR adding a duplicate fails here."""
    seen: set = set()
    duplicates: list = []
    for entry in V1_PUBLIC_API:
        if entry in seen:
            duplicates.append(entry)
        seen.add(entry)
    assert not duplicates, f"duplicate entries in V1_PUBLIC_API: {duplicates}"
