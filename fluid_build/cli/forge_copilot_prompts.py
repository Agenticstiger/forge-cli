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

"""Prompt builders for the LLM-backed Forge copilot."""

from __future__ import annotations

__all__ = [
    "build_clarification_system_prompt",
    "build_clarification_user_prompt",
    "build_system_prompt",
    "build_user_prompt",
]


import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from fluid_build.cli.forge_copilot_memory import CopilotMemorySnapshot
from fluid_build.schema_manager import FluidSchemaManager

from .forge_copilot_contract_helpers import _normalize_interview_summary


def _latest_fluid_version() -> str:
    """Return the newest bundled FLUID schema version."""
    return FluidSchemaManager.latest_bundled_version()


# Default-guidance directory under agent_specs/. Each ``.yaml`` file has
# a single top-level key ``system_prompt`` whose value is injected into
# one of the prompt builders at a labelled slot. Editing the YAML is the
# supported way to adjust the prose — no Python change needed — but the
# prompt tests lock the composed output and will fail if drift is
# unintentional.
_DEFAULTS_DIR: Path = Path(__file__).with_name("agent_specs") / "_defaults"


def _load_default_guidance() -> Mapping[str, str]:
    """Load ``_defaults/*.yaml`` into a {name: system_prompt_text} map.

    Invoked once at module import.  Missing files or missing
    ``system_prompt`` keys fall back to an empty string so that an
    incomplete install doesn't crash the CLI — the snapshot test
    catches such drift in CI.
    """
    guidance: dict[str, str] = {}
    if not _DEFAULTS_DIR.is_dir():
        return guidance
    for path in sorted(_DEFAULTS_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, Mapping):
            continue
        text = raw.get("system_prompt")
        if isinstance(text, str):
            guidance[path.stem] = text
    return guidance


_DEFAULT_GUIDANCE: Mapping[str, str] = _load_default_guidance()

_AUXILIARY_PROMPT_NAMES = frozenset({"clarification", "evaluation"})
_AUXILIARY_PROMPTS: Mapping[str, str] = {
    name: _DEFAULT_GUIDANCE.get(name, "") for name in _AUXILIARY_PROMPT_NAMES
}


def _render_auxiliary_prompt(name: str, replacements: Mapping[str, str]) -> str:
    text = _AUXILIARY_PROMPTS.get(name, "")
    for key, value in replacements.items():
        text = text.replace("${" + key + "}", value)
    return text


def _evaluation_prompt_spec() -> Mapping[str, Any]:
    raw = _AUXILIARY_PROMPTS.get("evaluation", "{}")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return spec if isinstance(spec, Mapping) else {}


# Per-technique rule sheets injected into the user prompt. Keyed by the
# canonical value of ``context["data_modeling_technique"]`` (produced by
# :func:`fluid_build.cli.forge_copilot_interview.normalize_interview_value`).
# The LLM reads this alongside ``upstream_products`` and must follow the
# named conventions in every ``additional_files`` SQL file it emits.
_MODELING_GUIDANCE: Mapping[str, Mapping[str, Any]] = {
    "data_vault_2": {
        "label": "Data Vault 2.0",
        "naming_conventions": {
            "hub": "hub_<entity>",
            "link": "lnk_<relation>",
            "satellite": "sat_<entity>_<source_system>",
            "point_in_time": "pit_<entity>",
            "bridge": "br_<relation>",
        },
        "key_strategy": (
            "Business keys are hashed to 32-hex surrogate keys via "
            "md5(upper(trim(<business_key>))). Parent keys in links and "
            "satellites reference hub hash keys only — never raw business keys."
        ),
        "load_metadata": [
            "load_dts TIMESTAMP — insert time of the record",
            "record_source VARCHAR — short identifier of the upstream source system",
            "hash_diff VARCHAR (satellites only) — md5 of all descriptive attributes",
        ],
        "layer_structure": (
            "Staging view per upstream source (one per consume). Raw vault: hubs + "
            "links + satellites. Business vault (optional) may derive PITs/bridges."
        ),
        "insert_only": True,
        "anti_patterns": [
            "Do NOT update or delete raw-vault rows — all history is insert-only.",
            "Do NOT mix sources in one satellite; one source = one satellite.",
            "Do NOT fabricate business keys when a natural key exists upstream.",
        ],
    },
    "dimensional": {
        "label": "Dimensional (Kimball)",
        "naming_conventions": {
            "staging": "stg_<source>",
            "dimension": "dim_<entity>",
            "fact": "fct_<grain>",
            "conformed_dimension": "dim_<conformed_entity>",
        },
        "key_strategy": (
            "Dimensions have integer surrogate keys (<entity>_key) generated via "
            "dbt_utils.generate_surrogate_key() from the natural key + scd columns. "
            "Facts reference dimension surrogate keys only — never natural keys."
        ),
        "load_metadata": [
            "valid_from TIMESTAMP — SCD type-2 effective start",
            "valid_to TIMESTAMP — SCD type-2 effective end (null for current row)",
            "is_current BOOLEAN — true for the live row of each natural key",
        ],
        "layer_structure": (
            "Staging view per upstream source, then conformed dimensions (shared "
            "across facts), then fact tables one per business process / grain."
        ),
        "scd_handling": (
            "Type-2 for dimensions with historical attributes; type-1 for rapidly "
            "changing dimensions where history isn't required."
        ),
        "anti_patterns": [
            "Do NOT reference natural keys from fact tables — only surrogate keys.",
            "Do NOT duplicate conformed dimensions per fact; reuse the shared dim.",
            "Do NOT store additive measures on dimensions; measures belong on facts.",
        ],
    },
}


def build_system_prompt(
    capability_matrix: Mapping[str, Any], known_build_engines: Sequence[str]
) -> str:
    """System prompt for structured FLUID contract generation."""
    providers = ", ".join(capability_matrix.get("providers") or [])
    engines = ", ".join(capability_matrix.get("build_engines") or list(known_build_engines))
    fv = _latest_fluid_version()
    return (
        # Security lint: this function constructs static prompt prose, not executable SQL.
        f"You are FLUID Forge Copilot. Generate a production-ready FLUID {fv} contract and README "  # noqa: S608
        "that only use locally supported templates, providers, and build engines.\n"
        "Return strict JSON only. Do not wrap the response in markdown fences.\n"
        "Never include secrets, access tokens, raw sample values, or verbatim file contents.\n"
        f"ALWAYS use fluidVersion '{fv}'.\n"
        "Treat project_memory as a soft preference layer only. Explicit user context and the current "
        "discovery report take precedence.\n"
        "Use interview_summary as the authoritative statement of current user intent.\n\n"
        "TEAM MEMORY: If team_memory is provided, treat it as authoritative team conventions:\n"
        "- Use team vocabulary (entities, measures, dimensions) as preferred names in the contract.\n"
        "- Apply team naming conventions (product_prefix, layer_convention, column_style).\n"
        "- Respect team defaults (provider, build_engine, domain, owner_team) unless the user overrides.\n"
        "- Honour team decisions — do not contradict architectural decisions the team has made.\n"
        "Team memory takes precedence over project_memory and personal defaults.\n\n"
        "If interview_summary includes canonical_model or supporting_standards, use them as the authoritative "
        "semantic modeling guidance for entity names, measures, dimensions, and descriptions.\n"
        "Prefer canonical business vocabulary from those standards over source-table or file-specific names.\n\n"
        # --- Chain-of-thought reasoning ---
        "REASONING: Before generating the contract, think through these steps in order:\n"
        "1. Analyze the data sources — what schema shape, column types, and relationships are implied?\n"
        "2. Evaluate which template best matches the use case and why.\n"
        "3. Select the provider and build engine based on the capability matrix and compatibility rules.\n"
        "4. Design entity modeling — identify primary keys, measures, dimensions, and time grains.\n"
        "5. Determine if sovereignty or agentPolicy blocks are needed based on compliance context.\n"
        "Include your reasoning in a top-level 'reasoning' key (string) in the response JSON.\n\n"
        "The JSON object must contain keys: reasoning, recommended_template, recommended_provider, "
        "recommended_patterns, architecture_suggestions, best_practices, technology_stack, "
        "description, domain, owner, readme_markdown, contract, additional_files.\n\n"
        f"CRITICAL: The contract value must be a JSON object that strictly conforms to the FLUID {fv} schema.\n"
        "The ONLY allowed top-level keys in the contract object are: "
        "fluidVersion, kind, id, name, description, domain, metadata, consumes, builds, exposes, sovereignty.\n"
        "Only include 'sovereignty' when the user specifies jurisdiction, compliance, or data residency requirements.\n"
        "DO NOT add 'quality', 'governance', 'owner', or any other top-level key.\n\n"
        "metadata must be an object with: owner (object with team and email) and layer.\n\n"
        "Each build must have: id, pattern (one of: 'embedded-logic', 'hybrid-reference', 'multi-stage'), "
        "engine (one of: " + engines + "), properties, execution.\n"
        "The 'engine' value MUST be exactly one of the short names above. "
        "Do NOT invent provider-suffixed variants like 'dbt-snowflake', 'dbt-bigquery', 'dbt-athena', "
        "'dbt-redshift', 'dataform', or 'glue' — those are NOT valid. "
        "For Snowflake/BigQuery/Athena dbt projects, use engine='dbt' and declare the target platform "
        "via binding.platform on each expose.\n"
        "BUILD PROPERTIES SHAPE (strict — additionalProperties is false per pattern):\n"
        "- pattern='hybrid-reference' (the common dbt case): properties = {model (required, string), "
        "vars? (object), materializations? (object, keys->{table|view|incremental|ephemeral}), "
        "tags?, labels?}. DO NOT add 'profile', 'projectDir', 'target', 'schema', 'database', or any "
        "other key to properties — those are resolved at apply time from the provider config, not "
        "declared in the contract.\n"
        "- pattern='embedded-logic' (engine='sql' or 'python'): properties = {sql (required, string), "
        "language? (one of: sql, flink_sql, pyspark, scala, python, r), parameters? (object), "
        "tags?, labels?}.\n"
        "- pattern='multi-stage': properties = {stages (array of objects with name, pattern, "
        "properties, dependsOn)}.\n"
        "For engine='sql', properties must contain 'sql' with a SQL string.\n"
        "For engine='python', the build must have 'repository' and properties.model.\n"
        "execution must have trigger (object with type and iterations) and runtime (object with platform and resources).\n"
        "trigger.type must be one of: 'cron' (time-based, e.g. daily at 2am), 'event' (data-arrival or webhook), "
        "'manual' (on-demand), or 'streaming' (continuous). trigger.iterations is usually 1 for batch, -1 for streaming.\n"
        "If the user asked for scheduling, set trigger.type='cron' and a sensible schedule in trigger.schedule (cron syntax).\n"
        "DO NOT add 'consumes' or 'produces' inside a build object.\n\n"
        "Each consume must have: productId (string) and exposeId (string). No other keys.\n\n"
        "Each expose must have: exposeId (string), kind (string), binding (object with platform, format, location), "
        "contract (object with schema as array of column objects with name, type, required).\n"
        "binding.platform is REQUIRED and must be one of: " + providers + ".\n"
        "binding.format is REQUIRED and must be one of: 'bigquery_table', 'snowflake_table', "
        "'gcs_file', 's3_file', 'http_api', 'grpc_api', 'pubsub_topic', 'kafka_topic', "
        "'delta_table', 'iceberg', 'parquet', 'csv', 'json', 'other'. "
        "Match the format to the platform: snowflake->'snowflake_table', gcp->'bigquery_table', "
        "aws->'s3_file' or 'delta_table' or 'iceberg', local->'parquet' or 'csv' or 'json'. "
        "Do NOT use generic values like 'table', 'view', or 'dataset'.\n"
        "DO NOT put 'platform' inside binding.location.\n\n"
        f"NEW IN {fv} — SEMANTICS BLOCK (required on each expose):\n"
        "Each expose MUST include a 'semantics' object with the following structure:\n"
        "- name (string): Human-readable name for this semantic model\n"
        "- description (string): Business context for what this model represents\n"
        "- entities (array): Join keys with type annotations. Each entity has: name (string), "
        "type (one of: 'primary', 'foreign', 'unique', 'natural'), and optional expr and description.\n"
        "- measures (array): Aggregatable expressions. Each measure has: name (string, required), "
        "agg (one of: 'sum', 'avg', 'count', 'count_distinct', 'min', 'max', 'median', 'percentile', required), "
        "and optional expr, description, createMetric (boolean).\n"
        "- dimensions (array): Grouping axes. Each dimension has: name (string, required), "
        "type (one of: 'categorical', 'time', required), and optional expr, description, "
        "typeParams (object with timeGranularity for time dimensions).\n"
        "- metrics (array): KPI definitions. Each metric has: name (string, required), "
        "type (one of: 'simple', 'derived', 'ratio', required), "
        "and optional measure (for simple), filter, inputMetrics (array of strings for derived/ratio), "
        "expr (for derived), numerator/denominator (for ratio), description.\n"
        "The semantics block enables AI agents and BI tools to generate correct queries without hallucination.\n\n"
        # --- Default guidance loaded from agent_specs/_defaults/ ---
        # These blocks are expanded from YAML at import time so editing
        # the prose doesn't require a Python change. The mid-prompt YAML
        # blocks end with one trailing newline from YAML ``|``; we add
        # one more newline per block to reproduce the original ``\n\n`` gap.
        + _DEFAULT_GUIDANCE.get("sovereignty", "")
        + "\n"
        + _DEFAULT_GUIDANCE.get("agent_policy", "")
        + "\n"
        + "Follow the seed_contract structure exactly as a reference for the correct schema shape.\n"  # noqa: S608  # nosec B608
        f"Allowed providers: {providers}.\n"
        "Only use build engines from the provided capability matrix.\n\n"
        # --- Upstream-driven transformation SQL ---
        + _DEFAULT_GUIDANCE.get("upstream_sql", "") + "\n"
        # --- Engine-owned files: do NOT recreate ---
        "ENGINE-OWNED FILES (do NOT write these to additional_files):\n"
        "- dbt_project/models/sources.yml — emitted by the engine. NEVER include a "
        "  `sources:` block in any YAML you ship; duplicating source declarations makes "
        '  dbt fail with "two sources with the same name". If you emit a schema.yml '
        "  file, it must contain ONLY a `models:` top-level key.\n"
        "- dbt_project/profiles.yml — emitted by the engine.\n"
        "- dbt_project/dbt_project.yml — emitted by the engine.\n"
        "When a modeling technique is active the engine does NOT emit any per-model "
        "schema.yml either, so you SHOULD ship exactly one schema.yml at "
        "`additional_files['dbt_project/models/schema.yml']` listing every staging + "
        "mart model you authored, with per-column tests.\n\n"
        # --- Modeling-technique mandate ---
        + _DEFAULT_GUIDANCE.get("technique_mandate", "") + "\n"
    )


_EVAL_MAX_SCHEMA_COLUMNS = 3


def _truncate_contract_for_eval(contract: Mapping[str, Any]) -> dict:
    """Return a lightweight copy of the contract for evaluation.

    Large schema arrays are truncated to the first few columns to keep
    the evaluation prompt small enough for the routing model.
    """
    c = dict(contract)
    exposes = c.get("exposes")
    if isinstance(exposes, list):
        trimmed = []
        for expose in exposes:
            expose = dict(expose)
            schema = (expose.get("contract") or {}).get("schema")
            if isinstance(schema, list) and len(schema) > _EVAL_MAX_SCHEMA_COLUMNS:
                expose = dict(expose)
                expose["contract"] = dict(expose.get("contract") or {})
                expose["contract"]["schema"] = schema[:_EVAL_MAX_SCHEMA_COLUMNS]
                expose["contract"]["_truncated_columns"] = len(schema)
            trimmed.append(expose)
        c["exposes"] = trimmed
    return c


def build_evaluation_prompt(
    context: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> str:
    """Build a prompt that asks the LLM to evaluate a generated contract.

    Used for self-evaluation after schema validation passes — checks
    semantic quality, not just structural correctness.  The response
    is a small JSON: ``{"score": int, "issues": [str], "suggestions": [str]}``.
    """
    goal = context.get("project_goal") or context.get("description") or ""
    use_case = context.get("use_case") or ""
    data_sources = context.get("data_sources") or ""
    prompt_spec = _evaluation_prompt_spec()
    return json.dumps(
        {
            "task": prompt_spec.get("task", ""),
            "user_requirements": {
                "project_goal": goal,
                "use_case": use_case,
                "data_sources": data_sources,
            },
            "contract": _truncate_contract_for_eval(contract),
            "evaluation_criteria": prompt_spec.get("evaluation_criteria", []),
            "response_format": prompt_spec.get("response_format", {}),
        },
        indent=2,
        sort_keys=True,
    )


def build_clarification_system_prompt(capability_matrix: Mapping[str, Any]) -> str:
    """System prompt for interview planning before contract generation."""
    providers = ", ".join(capability_matrix.get("providers") or [])
    templates = ", ".join(sorted((capability_matrix.get("templates") or {}).keys()))
    fv = _latest_fluid_version()
    return _render_auxiliary_prompt(
        "clarification",
        {
            "fluid_version": fv,
            "providers": providers,
            "templates": templates,
        },
    )


def build_clarification_user_prompt(
    *,
    interview_state: Mapping[str, Any],
    discovery_report: Any,
    capability_matrix: Mapping[str, Any],
    project_memory: Optional[CopilotMemorySnapshot] = None,
    team_memory: Optional[Mapping[str, Any]] = None,
    previous_failure: Sequence[str] | None = None,
) -> str:
    """Build the adaptive interview prompt payload."""
    payload: dict[str, Any] = {
        "interview_state": interview_state,
        "discovery_report": discovery_report.to_prompt_payload(),
        "capability_matrix": capability_matrix,
        "target_slots": [
            "project_goal",
            "use_case",
            "data_sources",
            "provider_hint",
            "domain",
            "canonical_model",
            "supporting_standards",
            "owner_team",
            "build_engine",
            "output_kind",
            "primary_entity",
            "primary_measures",
            "primary_dimensions",
            "time_dimension",
            "time_granularity",
            "refresh_cadence",
            "schedule_engine",
            "trigger_type",
            "consumes",
            "jurisdiction",
            "regulatory_framework",
            "data_sensitivity",
            "ai_access_policy",
        ],
        "priorities": [
            "Ask nothing if current context and discovery are already sufficient.",
            "Prefer semantic intent questions over generic project-management questions.",
            "If use_case is ambiguous, prefer the canonical taxonomy with an Other / Not sure option.",
            "Prefer inferring canonical_model and supporting_standards from domain-specific wording before asking an extra question.",
            "Assume the user may answer with fuzzy wording and use transcript raw_input plus resolved values together.",
            "If there was a generation failure, only ask questions that directly reduce that ambiguity.",
            (
                "If existing_products are listed and the user's project_goal is semantically similar to an existing product, "
                "flag it in your reason field and ask: 'This looks similar to <existing_id>. Are you extending it or creating something new?'"
            ),
            (
                "If the user explicitly wants scheduling or mentions DAGs, infer schedule_engine and trigger_type. "
                "Available schedulers: airflow, dagster, prefect. Default trigger_type is 'cron' for batch workloads. "
                "Do not ask an orchestration question after schedule_engine has already been answered."
            ),
            (
                "If the domain is healthcare, finance, or the user mentions compliance, GDPR, HIPAA, CCPA, or data residency, "
                "ask about jurisdiction and regulatory requirements. Canonical jurisdiction values: EU, US, UK, CA, AU, JP, Global."
            ),
            (
                "If data involves PII, PHI, or financial records, infer data_sensitivity as confidential or restricted "
                "and suggest agentPolicy constraints (canStore=false, deniedUseCases=[training, fine_tuning])."
            ),
        ],
    }
    if team_memory:
        payload["team_memory"] = team_memory
    if project_memory:
        payload["project_memory"] = project_memory.to_prompt_payload()
    if previous_failure:
        payload["previous_failure"] = list(previous_failure)

    # Inject domain-specific context if available in interview state
    interview_ctx = interview_state.get("normalized_context") or interview_state
    domain_expertise = (
        interview_ctx.get("domain_expertise") if isinstance(interview_ctx, dict) else None
    )
    if domain_expertise:
        payload["domain_expertise"] = domain_expertise
        # Surface domain questions as suggested topics
        domain_questions = domain_expertise.get("domain_questions")
        if domain_questions:
            payload["suggested_domain_questions"] = domain_questions

    return json.dumps(payload, indent=2, sort_keys=True)


def build_user_prompt(
    *,
    context: Mapping[str, Any],
    discovery_report: Any,
    capability_matrix: Mapping[str, Any],
    seed_contract: Mapping[str, Any],
    seed_template: str,
    seed_provider: str,
    attempt_index: int,
    previous_errors: Sequence[str],
    previous_payload: Optional[Mapping[str, Any]],
    project_memory: Optional[CopilotMemorySnapshot] = None,
    team_memory: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build the attempt-specific user prompt."""
    interview_summary = _normalize_interview_summary(context)

    # Inject industry skills into the prompt when available.
    # Slice UX-J: prefer the pre-compiled payload (already contains
    # only prompt-relevant fields).  Fall back to the legacy
    # on-the-fly extraction from the raw skills dict for backward
    # compatibility.
    compiled_skills = context.get("compiled_skills")
    skills = context.get("industry_skills")
    if compiled_skills:
        # Pre-compiled payload — already in the right shape for the prompt.
        interview_summary["industry_skills"] = compiled_skills
    elif skills:
        skills_hint: dict[str, Any] = {}
        ind = skills.get("industry", {})
        if ind.get("label"):
            skills_hint["industry"] = ind["label"]
        cm = skills.get("canonical_model", {})
        if cm.get("label"):
            skills_hint["canonical_model"] = cm["label"]
        domains = skills.get("domains")
        if domains:
            skills_hint["domains"] = [d.get("label", d.get("name")) for d in domains]
        compliance = skills.get("compliance")
        if compliance:
            skills_hint["compliance"] = compliance
            skills_hint["requires_sovereignty"] = True
        if skills_hint:
            interview_summary["industry_skills"] = skills_hint

    prompt: dict[str, Any] = {
        "attempt": attempt_index,
        "interview_summary": interview_summary,
        "capability_matrix": capability_matrix,
        "discovery_report": discovery_report.to_prompt_payload(),
        "seed_template": seed_template,
        "seed_provider": seed_provider,
        "seed_contract": seed_contract,
        "response_requirements": {
            "metadata_only_discovery": True,
            "include_additional_files_only_if_needed": True,
            "use_placeholder_env_vars_for_credentials": True,
            "prefer_manual_trigger_for_execute_compatibility": True,
            "generate_sovereignty_when_compliance_detected": True,
            "generate_agent_policy_for_sensitive_data": True,
        },
    }
    # Inject domain expertise (architecture, security, modeling standards) if detected
    domain_expertise = context.get("domain_expertise")
    if domain_expertise:
        prompt["domain_expertise"] = domain_expertise

    # Inject data modeling flag — tells LLM to generate richer semantic blocks + dbt models
    if context.get("data_modeling"):
        prompt["data_modeling_requested"] = True
        prompt["dbt_generation_instructions"] = (
            "Generate dbt model SQL files in additional_files. "
            "Use staging models (stg_ prefix) for source cleanup, "
            "fact tables (fct_ prefix) for events/transactions, "
            "and dimension tables (dim_ prefix) for entities. "
            "Include a schema.yml with column descriptions."
        )

    # Inject upstream product schemas — lets the LLM emit real dbt SQL
    # with correct source identifiers, join keys, and column projections
    # instead of TODO skeletons. Present only when upstream contracts
    # were discovered by ``_create_project_minimal``.
    upstream_products = context.get("upstream_products")
    if upstream_products:
        prompt["upstream_products"] = upstream_products

    # Data-modeling technique guidance. The interview bootstrap always
    # resolves this field to a canonical value (default ``data_vault_2``)
    # so the LLM prompt can unconditionally include the matching naming,
    # key-strategy and load-metadata rules. The guidance text is kept in
    # ``_MODELING_GUIDANCE`` rather than inside the system prompt so it
    # ships under the user prompt where it belongs (per-run context).
    technique = context.get("data_modeling_technique")
    if technique and technique in _MODELING_GUIDANCE:
        prompt["data_modeling_technique"] = technique
        prompt["data_modeling_guidance"] = _MODELING_GUIDANCE[technique]

    if team_memory:
        prompt["team_memory"] = team_memory
    if project_memory:
        prompt["project_memory"] = project_memory.to_prompt_payload()
    if previous_errors:
        prompt["repair_feedback"] = list(previous_errors)
    if previous_payload:
        prompt["previous_response_summary"] = {
            key: value
            for key, value in previous_payload.items()
            if key in {"recommended_template", "recommended_provider", "contract"}
        }
    return json.dumps(prompt, indent=2, sort_keys=True)
