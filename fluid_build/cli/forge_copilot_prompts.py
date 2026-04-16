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
from typing import Any, Mapping, Optional, Sequence

from fluid_build.cli.forge_copilot_memory import CopilotMemorySnapshot
from fluid_build.schema_manager import FluidSchemaManager

from .forge_copilot_contract_helpers import _normalize_interview_summary


def _latest_fluid_version() -> str:
    """Return the newest bundled FLUID schema version."""
    return FluidSchemaManager.latest_bundled_version()


def build_system_prompt(
    capability_matrix: Mapping[str, Any], known_build_engines: Sequence[str]
) -> str:
    """System prompt for structured FLUID contract generation."""
    providers = ", ".join(capability_matrix.get("providers") or [])
    engines = ", ".join(capability_matrix.get("build_engines") or list(known_build_engines))
    fv = _latest_fluid_version()
    return (
        f"You are FLUID Forge Copilot. Generate a production-ready FLUID {fv} contract and README "
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
        # --- Sovereignty block guidance ---
        "SOVEREIGNTY BLOCK (optional — include when user specifies compliance, jurisdiction, or data residency):\n"
        "The contract MAY include a top-level 'sovereignty' object with:\n"
        "- jurisdiction (enum): EU, US, UK, CA, AU, JP, CN, IN, BR, Global, Multi-Region\n"
        "- allowedRegions (array of strings): Cloud regions where data may reside\n"
        "- deniedRegions (array of strings): Cloud regions explicitly prohibited\n"
        "- dataResidency (boolean, default true): Data must stay within jurisdiction\n"
        "- crossBorderTransfer (boolean, default false): Whether cross-border transfer is allowed\n"
        "- transferMechanisms (array, enum): SCCs, BCRs, Adequacy, DPF, Consent, Derogation\n"
        "- regulatoryFramework (array, enum): GDPR, CCPA, CPRA, HIPAA, PIPEDA, LGPD, PDPA, POPIA, DPA, APPI\n"
        "- enforcementMode (enum, default strict): strict, advisory, audit\n"
        "Include sovereignty when the user mentions: GDPR, HIPAA, CCPA, data residency, EU-only, compliance, "
        "regulated data, PII, PHI, jurisdiction, or regional restrictions.\n"
        "Match allowedRegions to the chosen provider (e.g. eu-west-1/eu-central-1 for AWS in EU, "
        "europe-west1/europe-west3 for GCP in EU).\n\n"
        # --- Agent policy guidance ---
        "AGENT POLICY (optional — include when data has sensitivity or AI access restrictions):\n"
        "Each expose MAY include 'policy.agentPolicy' with:\n"
        "- allowedModels (array): AI models permitted to consume this data (e.g. gpt-4, claude-3-opus)\n"
        "- deniedModels (array): AI models explicitly blocked\n"
        "- allowedUseCases (array, enum): inference, reasoning, analysis, summarization, classification, "
        "embedding, search, qa, code_generation, fine_tuning, training, rag\n"
        "- deniedUseCases (array, enum): Same enum values, explicitly blocked use cases\n"
        "- canStore (boolean, default false): Whether AI systems can cache/persist data\n"
        "- canReason (boolean, default false): Whether multi-step reasoning is allowed\n"
        "- maxTokensPerRequest (integer): Per-request token limit\n"
        "- retentionPolicy: {maxRetentionDays: int, requireDeletion: bool}\n"
        "- auditRequired (boolean, default true): Whether AI access must be logged\n"
        "- purposeLimitation (string): Free-text scope of allowed purpose\n"
        "Include agentPolicy when the data involves: PII, PHI, financial data, regulated data, "
        "or when the user mentions AI access controls, model restrictions, or data sensitivity.\n"
        "Default to restrictive settings: canStore=false, canReason=false, "
        "deniedUseCases=[training, fine_tuning], auditRequired=true.\n\n"
        "Follow the seed_contract structure exactly as a reference for the correct schema shape.\n"
        f"Allowed providers: {providers}.\n"
        "Only use build engines from the provided capability matrix."
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
    return json.dumps(
        {
            "task": "Evaluate this FLUID contract against the user's requirements.",
            "user_requirements": {
                "project_goal": goal,
                "use_case": use_case,
                "data_sources": data_sources,
            },
            "contract": _truncate_contract_for_eval(contract),
            "evaluation_criteria": [
                "Completeness: Does the contract cover all data sources and use cases mentioned?",
                "Entity modeling: Are primary keys, measures, dimensions, and time grains sensible?",
                "Production-readiness: Are bindings, triggers, and engine choices appropriate?",
                "Semantic quality: Are names, descriptions, and business vocabulary clear?",
            ],
            "response_format": {
                "score": "integer 1-10 (10 = excellent)",
                "issues": "list of specific problems found (empty if score >= 7)",
                "suggestions": "list of concrete improvements",
            },
        },
        indent=2,
        sort_keys=True,
    )


def build_clarification_system_prompt(capability_matrix: Mapping[str, Any]) -> str:
    """System prompt for interview planning before contract generation."""
    providers = ", ".join(capability_matrix.get("providers") or [])
    templates = ", ".join(sorted((capability_matrix.get("templates") or {}).keys()))
    fv = _latest_fluid_version()
    return (
        "You are FLUID Forge Copilot Interview Planner.\n"
        f"Your job is to ask the fewest high-signal questions needed to generate a strong FLUID {fv} contract.\n"
        "Return strict JSON only. Do not use markdown fences.\n"
        "Never ask for secrets, passwords, API keys, access tokens, or raw credentials.\n"
        "Use discovery and project memory as context, but explicit current-run user input takes precedence.\n"
        "Ask at most 2 questions in a round. Prefer choices when the taxonomy is stable.\n"
        "Users may answer imperfectly with partial phrases, synonyms, abbreviations, or adjacent concepts.\n"
        "Treat transcript.raw_input as primary evidence of user intent and transcript.resolved_value as a helpful local guess.\n"
        "If local matching is uncertain, prefer inferring from the raw wording over asking a rigid repeat question.\n"
        "Canonical use_case values: analytics, etl_pipeline, streaming, ml_pipeline, data_platform, other.\n"
        "Canonical schedule_engine values: airflow, dagster, prefect.\n"
        "Canonical trigger_type values: cron, event, manual, streaming.\n"
        "Canonical jurisdiction values: EU, US, UK, CA, AU, JP, CN, IN, BR, Global, Multi-Region.\n"
        "Canonical regulatory_framework values: GDPR, CCPA, CPRA, HIPAA, PIPEDA, LGPD, PDPA, POPIA, DPA, APPI.\n"
        "Canonical data_sensitivity values: public, internal, confidential, restricted.\n"
        "Canonical model values include: tmf_sid, nrf_arts, gs1_gdm, adobe_xdm, hl7_fhir, omop_cdm.\n"
        "Supporting standards include: gs1_gdm, gs1_epcis_cbv.\n"
        "For telco, retail, and healthcare requests, infer modeling standards from the raw wording whenever possible "
        "and only ask a modeling-standard question when the choice is still ambiguous.\n"
        "Allowed providers: " + providers + ". Known templates: " + templates + ".\n"
        "Return a JSON object with keys: status, reason, context_patch, assumptions, questions.\n"
        "status must be either 'ask' or 'ready'.\n"
        "questions must be an array of objects with: id, field, prompt, type, choices, required, allow_skip.\n"
        "Supported question types are 'text' and 'choice'.\n"
        "Use context_patch to normalize obvious values from existing evidence.\n"
        "Use assumptions only for bounded defaults that are safe to surface to the user.\n"
        "Mark status='ready' when enough intent is known to generate a defensible contract without more questioning.\n"
        "If existing_products are listed in the interview state and the user's project_goal is semantically similar "
        "to an existing product, flag it in your reason and ask whether they are extending it or creating something new."
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
            "If existing_products are listed and the user's project_goal is semantically similar to an existing product, "
            "flag it in your reason field and ask: 'This looks similar to <existing_id>. Are you extending it or creating something new?'",
            "If the user mentioned scheduling, DAGs, orchestration, or pipelines, infer schedule_engine and trigger_type. "
            "Available schedulers: airflow, dagster, prefect. Default trigger_type is 'cron' for batch workloads.",
            "If the domain is healthcare, finance, or the user mentions compliance, GDPR, HIPAA, CCPA, or data residency, "
            "ask about jurisdiction and regulatory requirements. Canonical jurisdiction values: EU, US, UK, CA, AU, JP, Global.",
            "If data involves PII, PHI, or financial records, infer data_sensitivity as confidential or restricted "
            "and suggest agentPolicy constraints (canStore=false, deniedUseCases=[training, fine_tuning]).",
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
