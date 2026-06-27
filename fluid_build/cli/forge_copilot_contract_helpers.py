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

"""Pure helper logic for the Forge copilot runtime."""

from __future__ import annotations

__all__ = [
    "KNOWN_BUILD_ENGINES",
    "PROVIDER_ENGINE_COMPATIBILITY",
    "TEMPLATE_ALIASES",
    "build_seed_contract",
    "classify_generation_failure",
    "extract_json_object",
    "normalize_generation_payload",
    "normalize_provider_name",
    "normalize_template_name",
    "redact_secret_like_text",
    "sanitize_additional_files",
    "sanitize_name",
    "validate_generated_result",
]


import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import yaml

from fluid_build.cli.forge_copilot_memory import CopilotMemorySnapshot
from fluid_build.cli.forge_copilot_taxonomy import (
    CANONICAL_MODEL_LABELS,
    SUPPORTING_STANDARD_LABELS,
    format_use_case_label,
    normalize_canonical_model,
    normalize_copilot_context,
    normalize_supporting_standards,
    normalize_use_case,
)
from fluid_build.util.safe_yaml import load_yaml_safe

SAFE_ADDITIONAL_FILE_EXTENSIONS = {
    ".py",
    ".sql",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".sh",
}

TEMPLATE_ALIASES = {
    "analytics-dashboard": "analytics",
    "analytics_dashboard": "analytics",
    "analytics_basic": "analytics",
    "analytics-basic": "analytics",
    "analytics": "analytics",
    "etl": "etl_pipeline",
    "etl-pipeline": "etl_pipeline",
    "etl_pipeline": "etl_pipeline",
    "ml-pipeline": "ml_pipeline",
    "ml_pipeline": "ml_pipeline",
    "ml": "ml_pipeline",
    "starter": "starter",
    "startertemplate": "starter",
    "streaming": "streaming",
    "streaming-pipeline": "streaming",
    "streaming_pipeline": "streaming",
}

KNOWN_BUILD_ENGINES = {
    # Transformation engines (ADP / CDP role).
    "sql",
    "python",
    "dbt",
    "spark",
    "custom",
    # Ingestion engines (SDP role) — must be advertised so the AI
    # path can pick the right engine for source-aligned products.
    # The build_runners directory ships dispatchers for each.
    "duckdb",
    "dlt",
    "airbyte",
    "meltano",
    "kafka-connect",
    "debezium",
}

PROVIDER_ENGINE_COMPATIBILITY = {
    # Local: every engine works (zero-infra ingestion + local dbt).
    "local": {
        "sql",
        "python",
        "dbt",
        "duckdb",
        "dlt",
        "airbyte",
        "meltano",
        "kafka-connect",
        "debezium",
    },
    # Cloud platforms: transformation engines + ingestion engines that
    # have first-class support there.
    "gcp": {"sql", "python", "dbt", "dlt", "airbyte", "meltano", "kafka-connect"},
    "aws": {"sql", "python", "dbt", "dlt", "airbyte", "meltano", "kafka-connect", "debezium"},
    "snowflake": {"sql", "python", "dbt", "dlt", "airbyte", "meltano"},
}

_AMBIGUITY_ERROR_KEYWORDS = (
    "semantics",
    "entity",
    "measure",
    "dimension",
    "metric",
    "business context",
    "description",
    "timegranularity",
)
_STRUCTURAL_ERROR_KEYWORDS = (
    "unsupported provider",
    "unsupported template",
    "binding.platform",
    "missing an engine",
    "not supported for provider",
    "must define repository",
    "must include sql",
    "parse error",
)


# Maps keyword patterns in error strings to repair guidance.
_REPAIR_GUIDANCE: List[Dict[str, Any]] = [
    {
        "pattern": "binding.platform",
        "category": "missing_field",
        "fix_hint": "Add 'platform' to each expose binding using one of the allowed providers.",
        "example": '{"platform": "local", "format": "parquet", "location": {"path": "..."}}',
    },
    {
        "pattern": "not supported for provider",
        "category": "engine_incompatible",
        "fix_hint": "Check the capability matrix — the chosen engine is not compatible with the provider.",
    },
    {
        "pattern": "unsupported provider",
        "category": "invalid_value",
        "fix_hint": "Use only providers listed in the capability matrix.",
    },
    {
        "pattern": "unsupported template",
        "category": "invalid_value",
        "fix_hint": "Use only templates listed in the capability matrix.",
    },
    {
        "pattern": "missing an engine",
        "category": "missing_field",
        "fix_hint": "Every build must have an 'engine' field (sql, python, or dbt).",
    },
    {
        "pattern": "must include sql",
        "category": "missing_field",
        "fix_hint": "For engine='sql', properties must contain a 'sql' key with a SQL string.",
        "example": '{"sql": "SELECT 1 AS id"}',
    },
    {
        "pattern": "must define repository",
        "category": "missing_field",
        "fix_hint": "For engine='python', the build must have a 'repository' field.",
    },
    {
        "pattern": "semantics",
        "category": "semantics_incomplete",
        "fix_hint": "Each expose needs a 'semantics' block with name, description, entities, measures, and dimensions.",
    },
    {
        "pattern": "is a required property",
        "category": "missing_field",
        "fix_hint": "A required field is missing — add the named property to the object.",
    },
    {
        "pattern": "is not valid",
        "category": "invalid_value",
        "fix_hint": "The value does not match the schema. Check allowed enum values or types.",
    },
    {
        "pattern": "data_modeling_technique=data_vault_2",
        "category": "modeling_technique_mismatch",
        "fix_hint": (
            "For Data Vault 2.0 you must emit at least one hub_ or sat_ model "
            "in additional_files under dbt_project/models/staging/. Reference "
            "hub hash keys from links, keep raw-vault inserts-only, and stamp "
            "load_dts + record_source on every row."
        ),
        "example": (
            '{"additional_files": {'
            '"dbt_project/models/staging/hub_party_source.sql": "...", '
            '"dbt_project/models/staging/sat_party_source_raw.sql": "...", '
            '"dbt_project/models/marts/lnk_<mart>.sql": "..."}}'
        ),
    },
    {
        "pattern": "data_modeling_technique=dimensional",
        "category": "modeling_technique_mismatch",
        "fix_hint": (
            "For dimensional modeling you must emit at least one dim_ or fct_ "
            "model in additional_files under dbt_project/models/marts/. Use "
            "dbt_utils.generate_surrogate_key() for dimension keys and "
            "reference those surrogates from fact tables."
        ),
        "example": (
            '{"additional_files": {'
            '"dbt_project/models/marts/dim_customer.sql": "...", '
            '"dbt_project/models/marts/fct_subscription_events.sql": "..."}}'
        ),
    },
    {
        "pattern": "declares `sources:`",
        "category": "engine_owned_file_collision",
        "fix_hint": (
            "The engine emits dbt_project/models/sources.yml from the "
            "upstream contracts — never include a `sources:` block in any "
            "YAML you ship. Remove the `sources:` block from the offending "
            "file; keep only `models:` (one top-level list of model tests)."
        ),
        "example": (
            '{"additional_files": {'
            '"dbt_project/models/schema.yml": '
            '"version: 2\\nmodels:\\n  - name: hub_party\\n    columns: [...]"}}'
        ),
    },
]


def build_structured_repair_feedback(
    errors: Sequence[str],
) -> List[Dict[str, Any]]:
    """Convert raw validation errors into structured repair guidance.

    Each error is annotated with a category, a concrete fix hint, and an
    optional JSON example.  The original error text is always preserved
    so the LLM can still interpret novel errors.
    """
    feedback: List[Dict[str, Any]] = []
    for error in errors:
        entry: Dict[str, Any] = {"error": error, "category": "schema_violation", "fix_hint": ""}
        lower = error.lower()
        for guidance in _REPAIR_GUIDANCE:
            if guidance["pattern"] in lower:
                entry["category"] = guidance["category"]
                entry["fix_hint"] = guidance["fix_hint"]
                if "example" in guidance:
                    entry["example"] = guidance["example"]
                break
        feedback.append(entry)
    return feedback


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").replace("\n", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _normalize_consumes_for_generation(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        product_id = str(item.get("productId") or item.get("product_id") or "").strip()
        expose_id = str(item.get("exposeId") or item.get("expose_id") or "").strip()
        if product_id and expose_id:
            normalized.append({"productId": product_id, "exposeId": expose_id})
    return normalized


def _normalize_interview_summary(context: Mapping[str, Any]) -> Dict[str, Any]:
    normalized_context = normalize_copilot_context(dict(context))
    summary = context.get("interview_summary")
    if isinstance(summary, Mapping):
        normalized = dict(summary)
    else:
        normalized = {
            "project_goal": normalized_context.get("project_goal"),
            "use_case": normalize_use_case(normalized_context.get("use_case")),
            "use_case_other": normalized_context.get("use_case_other"),
            "use_case_label": format_use_case_label(
                normalized_context.get("use_case"),
                normalized_context.get("use_case_other"),
            ),
            "data_sources": normalized_context.get("data_sources"),
            "provider_hint": normalized_context.get("provider")
            or normalized_context.get("provider_hint"),
            "domain": normalized_context.get("domain"),
            "canonical_model": normalized_context.get("canonical_model"),
            "supporting_standards": normalized_context.get("supporting_standards") or [],
            "owner_team": normalized_context.get("owner_team") or normalized_context.get("owner"),
            "build_engine": normalized_context.get("build_engine"),
            "output_kind": normalized_context.get("output_kind"),
            "semantic_intent": {
                "primary_entity": normalized_context.get("primary_entity"),
                "primary_measures": _coerce_string_list(normalized_context.get("primary_measures")),
                "primary_dimensions": _coerce_string_list(
                    normalized_context.get("primary_dimensions")
                ),
                "time_dimension": normalized_context.get("time_dimension"),
                "time_granularity": normalized_context.get("time_granularity"),
            },
            "refresh_cadence": normalized_context.get("refresh_cadence"),
            "consumes": normalized_context.get("consumes") or [],
            "assumptions": list(normalized_context.get("assumptions_used") or []),
            "answered_fields": sorted(
                key
                for key in (
                    "project_goal",
                    "use_case",
                    "data_sources",
                    "provider",
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
                    "consumes",
                )
                if normalized_context.get(key)
            ),
        }

    semantic_intent = normalized.get("semantic_intent")
    if not isinstance(semantic_intent, Mapping):
        semantic_intent = {}
    normalized["semantic_intent"] = {
        "primary_entity": semantic_intent.get("primary_entity")
        or normalized_context.get("primary_entity"),
        "primary_measures": _coerce_string_list(
            semantic_intent.get("primary_measures") or normalized_context.get("primary_measures")
        ),
        "primary_dimensions": _coerce_string_list(
            semantic_intent.get("primary_dimensions")
            or normalized_context.get("primary_dimensions")
        ),
        "time_dimension": semantic_intent.get("time_dimension")
        or normalized_context.get("time_dimension"),
        "time_granularity": semantic_intent.get("time_granularity")
        or normalized_context.get("time_granularity"),
    }
    normalized["canonical_model"] = normalize_canonical_model(
        normalized.get("canonical_model") or normalized_context.get("canonical_model")
    )
    normalized["supporting_standards"] = normalize_supporting_standards(
        normalized.get("supporting_standards") or normalized_context.get("supporting_standards")
    )
    normalized["use_case"] = normalize_use_case(normalized.get("use_case")) or normalized.get(
        "use_case"
    )
    normalized["use_case_label"] = normalized.get("use_case_label") or format_use_case_label(
        normalized.get("use_case"),
        normalized.get("use_case_other"),
    )
    normalized["consumes"] = _normalize_consumes_for_generation(
        normalized.get("consumes") or context.get("consumes")
    )
    normalized["answered_fields"] = sorted(
        set(normalized.get("answered_fields") or [])
        | {
            key
            for key in (
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
                "refresh_cadence",
            )
            if normalized.get(key)
        }
    )
    return normalized


def sanitize_name(value: Any) -> str:
    """Create a filesystem-safe identifier."""
    text = str(value or "copilot-data-product").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "copilot-data-product"


def _modeling_description_suffix(
    canonical_model: Optional[str], supporting_standards: Sequence[str]
) -> str:
    labels: List[str] = []
    if canonical_model:
        labels.append(CANONICAL_MODEL_LABELS.get(canonical_model, canonical_model))
    supporting_labels = [
        SUPPORTING_STANDARD_LABELS.get(standard, standard) for standard in supporting_standards
    ]
    if supporting_labels:
        if labels:
            return f" Modeled using {labels[0]} with supporting standards {', '.join(supporting_labels)}."
        return f" Modeled using supporting standards {', '.join(supporting_labels)}."
    if labels:
        return f" Modeled using {labels[0]}."
    return ""


def normalize_template_name(value: Any) -> str:
    """Normalize template aliases to registered template names."""
    if value is None:
        return "starter"
    normalized = str(value).strip().lower().replace("-", "_")
    return TEMPLATE_ALIASES.get(normalized, normalized)


def normalize_provider_name(value: Any) -> str:
    """Normalize infrastructure provider aliases (gcp, aws, local, snowflake)."""
    if value is None:
        return "local"
    return str(value).strip().lower().replace("-", "_")


def redact_secret_like_text(text: str, redact_secrets_fn: Callable[[str], str]) -> str:
    """Best-effort redaction for common secret patterns."""
    return redact_secrets_fn(text)


def _build_semantics_from_interview_summary(
    *,
    columns: List[Dict[str, Any]],
    interview_summary: Mapping[str, Any],
    expose_name: str,
    description: str,
) -> Dict[str, Any]:
    semantic_intent = interview_summary.get("semantic_intent")
    if not isinstance(semantic_intent, Mapping):
        semantic_intent = {}

    canonical_model = normalize_canonical_model(interview_summary.get("canonical_model"))
    supporting_standards = normalize_supporting_standards(
        interview_summary.get("supporting_standards")
    )
    default_entity_by_model = {
        "tmf_sid": "party",
        "nrf_arts": "retail_transaction",
        "gs1_gdm": "trade_item",
        "adobe_xdm": "experience_event",
        "hl7_fhir": "patient",
        "omop_cdm": "person",
    }
    source_entity_name = str(columns[0]["name"]).strip()
    default_entity_name = source_entity_name
    if (
        canonical_model
        and source_entity_name == "id"
        and len(columns) == 1
        and canonical_model in default_entity_by_model
    ):
        default_entity_name = default_entity_by_model[canonical_model]

    entity_name = str(semantic_intent.get("primary_entity") or default_entity_name).strip()
    entity_expr = source_entity_name if entity_name != source_entity_name else None
    measure_names = _coerce_string_list(semantic_intent.get("primary_measures"))
    if not measure_names:
        measure_names = [f"{entity_name}_count"]
    dimension_names = _coerce_string_list(semantic_intent.get("primary_dimensions"))
    time_dimension = str(semantic_intent.get("time_dimension") or "").strip()
    time_granularity = str(semantic_intent.get("time_granularity") or "").strip()

    entities = [{"name": entity_name, "type": "primary"}]
    if entity_expr:
        entities[0]["expr"] = entity_expr
        entities[0][
            "description"
        ] = f"Canonical {entity_name} entity mapped from {source_entity_name}."
    measures = []
    metrics = []
    for measure_name in measure_names:
        normalized_measure = sanitize_name(measure_name).replace("-", "_")
        if normalized_measure.endswith("_count"):
            agg = "count"
            expr = entity_expr or entity_name
        else:
            agg = "sum"
            expr = measure_name
        measures.append(
            {
                "name": normalized_measure,
                "agg": agg,
                "expr": expr,
                "description": f"{measure_name} for {expose_name}.",
            }
        )
        metrics.append(
            {
                "name": f"{normalized_measure}_metric",
                "type": "simple",
                "measure": normalized_measure,
                "description": f"Metric for {measure_name}.",
            }
        )

    dimensions = [{"name": entity_name, "type": "categorical"}]
    if entity_expr:
        dimensions[0]["expr"] = entity_expr
    for dimension_name in dimension_names:
        normalized_dimension = sanitize_name(dimension_name).replace("-", "_")
        if normalized_dimension != entity_name:
            dimensions.append({"name": normalized_dimension, "type": "categorical"})
    if time_dimension:
        time_dimension_entry: Dict[str, Any] = {"name": time_dimension, "type": "time"}
        if time_granularity:
            time_dimension_entry["typeParams"] = {"timeGranularity": time_granularity}
        dimensions.append(time_dimension_entry)

    semantic_description = description or "Semantic model for the exposed data product."
    semantic_description += _modeling_description_suffix(canonical_model, supporting_standards)

    return {
        "name": expose_name.replace("_", " ").title(),
        "description": semantic_description,
        "entities": entities,
        "measures": measures,
        "dimensions": dimensions,
        "metrics": metrics,
    }


def _default_binding(provider_name: str, expose_name: str) -> Dict[str, Any]:
    if provider_name == "gcp":
        return {
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {
                "project": "${FLUID_GCP_PROJECT}",
                "dataset": "analytics",
                "table": expose_name,
            },
        }
    if provider_name == "aws":
        return {
            "platform": "aws",
            "format": "parquet",
            "location": {
                "bucket": "${FLUID_AWS_BUCKET}",
                "key": f"runtime/out/{expose_name}.parquet",
                "region": "${AWS_REGION}",
            },
        }
    if provider_name == "snowflake":
        return {
            "platform": "snowflake",
            "format": "table",
            "location": {
                "database": "${SNOWFLAKE_DATABASE}",
                "schema": "ANALYTICS",
                "table": expose_name.upper(),
            },
        }
    return {
        "platform": "local",
        "format": "csv",
        "location": {"path": f"runtime/out/{expose_name}.csv"},
    }


def _seed_contract_override(context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Refine-mode seed: when ``context['seed_contract_override']`` is
    populated (set by ``_run_refine_interview``), the seed IS the
    existing contract — the LLM's job is to apply the user's change
    request to it, not author a new one."""
    override = context.get("seed_contract_override")
    if isinstance(override, dict) and override.get("kind") == "DataProduct":
        return dict(override)
    return None


def _seed_metadata(context: Mapping[str, Any], owner_team: str) -> Dict[str, Any]:
    """Build the metadata block for the seed contract.

    Honors ``--data-product-type`` (Phase 1) by reading the canonical
    pair from the registry — when the user said ``--data-product-type
    ADP`` the seed lands as ``layer: Silver`` + ``productType: ADP``.
    Defaults to Bronze/SDP when no type is specified.
    """
    from fluid_build.forge.product_types import (
        get_product_type,
    )

    requested = (
        context.get("data_product_type") or context.get("productType") or context.get("layer")
    )
    pt = get_product_type(str(requested)) if requested else None
    if pt is not None:
        layer = pt.layer
        product_type = pt.code
    elif requested in {"Platinum"}:
        # Platinum has no productType analogue — leave it absent.
        layer = "Platinum"
        product_type = None
    else:
        layer = "Bronze"
        product_type = "SDP"

    metadata: Dict[str, Any] = {
        "layer": layer,
        "owner": {"team": owner_team, "email": "data-team@example.com"},
    }
    if product_type:
        metadata["productType"] = product_type
    return metadata


def build_seed_contract(
    *,
    context: Mapping[str, Any],
    discovery_report: Any,
    template_name: str,
    provider_name: str,
    project_memory: Optional[CopilotMemorySnapshot],
    map_inferred_type_fn: Callable[[str], str],
) -> Dict[str, Any]:
    """Create a minimal valid 0.7.2 contract used as guidance for the LLM.

    Phase 0.4 / refine-mode short-circuit: when the interview loaded an
    existing contract via ``--refine``, ``_seed_contract_override``
    returns it verbatim so the LLM modifies the actual user contract
    instead of inventing a new one.
    """
    # Lazy import keeps the heavy ``jsonschema`` dependency off the
    # ``fluid --help`` / ``build_parser()`` cold path (this module is reached
    # transitively from ``fluid ai``'s registration via forge_copilot_runtime).
    from fluid_build.schema_manager import FluidSchemaManager

    override = _seed_contract_override(context)
    if override is not None:
        return override
    interview_summary = _normalize_interview_summary(context)
    project_name = sanitize_name(context.get("project_goal") or "copilot-data-product")
    expose_name = f"{project_name}_output"
    columns = [{"name": "id", "type": "integer", "required": True}]
    if discovery_report.sample_files:
        first = discovery_report.sample_files[0]
        columns = []
        for column_name, column_type in (first.get("columns") or {}).items():
            columns.append(
                {
                    "name": column_name,
                    "type": map_inferred_type_fn(column_type),
                    "required": False,
                }
            )
        if not columns:
            columns = [{"name": "id", "type": "integer", "required": True}]

    build_engine = str(interview_summary.get("build_engine") or "sql").strip().lower()
    if build_engine not in KNOWN_BUILD_ENGINES:
        build_engine = "sql"

    # Phase 1: when the user explicitly chose an SDP product type, the
    # seed should demonstrate the ``acquisition`` pattern (which the
    # LLM otherwise tries to fake with ``embedded-logic`` + python
    # scripts that fail validation). Building from the canonical
    # registry keeps every authoring path byte-equivalent (I2).
    requested_pt = (
        context.get("data_product_type") or context.get("productType") or context.get("layer")
    )
    _is_sdp_seed = False
    if requested_pt:
        from fluid_build.forge.product_types import get_product_type as _resolve_pt

        _resolved = _resolve_pt(str(requested_pt))
        _is_sdp_seed = _resolved is not None and _resolved.code == "SDP"

    if _is_sdp_seed:
        # Acquisition pattern: source.kind + source.mode are required
        # by acquisitionSource. Trigger type is ``schedule`` (not
        # ``scheduled``); cron expression goes alongside.
        first_source = ""
        sources = context.get("data_sources") or []
        if isinstance(sources, list) and sources:
            first_source = str(sources[0])
        source_block: Dict[str, Any] = {"kind": "filesystem", "mode": "full_refresh"}
        if first_source:
            source_block["connection"] = {"uri": first_source}
        build = {
            "id": "main_acquisition",
            "pattern": "acquisition",
            "engine": "duckdb",
            "properties": {"source": source_block},
            "execution": {
                "trigger": {"type": "schedule", "cron": "0 6 * * *"},
                "runtime": {
                    "platform": provider_name,
                    "resources": {"cpu": "1", "memory": "2Gi"},
                },
            },
        }
    else:
        build = {
            "id": "main_build",
            "pattern": "embedded-logic",
            "engine": "python" if build_engine == "python" else "sql",
            "execution": {
                "trigger": {"type": "manual"},
                "runtime": {
                    "platform": provider_name,
                    "resources": {"cpu": "1", "memory": "2Gi"},
                },
            },
        }
        if build["engine"] == "python":
            build["repository"] = "src/main.py"
            build["properties"] = {"model": "src.main:build"}
        else:
            build["properties"] = {"sql": "SELECT 1 AS id"}

    description = context.get("project_goal") or "AI-generated FLUID data product"
    domain = (
        context.get("domain")
        or interview_summary.get("domain")
        or (project_memory.preferred_domain if project_memory else None)
        or context.get("use_case")
        or "analytics"
    )
    owner_team = (
        interview_summary.get("owner_team")
        or context.get("owner_team")
        or context.get("owner")
        or (project_memory.preferred_owner if project_memory else None)
        or context.get("team_size")
        or "data-team"
    )
    # Compose-mode: ``_run_compose_interview`` pre-fills consumes[] on
    # the context with rows shaped as ``{productId, exposeId}`` (the
    # v0.7.3 schema shape). Prefer that over the legacy interview
    # summary which carried a free-form list.
    consumes = context.get("consumes") or interview_summary.get("consumes") or []
    if not isinstance(consumes, list):
        consumes = []

    # --- Build expose with optional agentPolicy stub ---
    expose: Dict[str, Any] = {
        "exposeId": expose_name,
        "kind": str(interview_summary.get("output_kind") or "table"),
        "binding": _default_binding(provider_name, expose_name),
        "contract": {"schema": columns},
        "semantics": _build_semantics_from_interview_summary(
            columns=columns,
            interview_summary=interview_summary,
            expose_name=expose_name,
            description=description,
        ),
    }

    data_sensitivity = context.get("data_sensitivity") or interview_summary.get("data_sensitivity")
    if data_sensitivity in ("confidential", "restricted"):
        expose["policy"] = {
            "agentPolicy": {
                "deniedUseCases": ["training", "fine_tuning"],
                "canStore": False,
                "canReason": False,
                "auditRequired": True,
            }
        }

    seed: Dict[str, Any] = {
        "fluidVersion": FluidSchemaManager.latest_bundled_version(),
        "kind": "DataProduct",
        "id": f"generated.{project_name}",
        "name": project_name.replace("-", " ").title(),
        "description": description,
        "domain": domain,
        "metadata": _seed_metadata(context, owner_team),
        "consumes": consumes,
        "builds": [build],
        "exposes": [expose],
    }

    # --- Optional sovereignty stub ---
    jurisdiction = context.get("jurisdiction") or interview_summary.get("jurisdiction")
    regulatory = context.get("regulatory_framework") or interview_summary.get(
        "regulatory_framework"
    )
    if jurisdiction or regulatory:
        sovereignty: Dict[str, Any] = {
            "enforcementMode": "strict",
        }
        if jurisdiction:
            sovereignty["jurisdiction"] = jurisdiction
        if regulatory:
            frameworks = regulatory if isinstance(regulatory, list) else [regulatory]
            sovereignty["regulatoryFramework"] = frameworks
        seed["sovereignty"] = sovereignty

    return seed


def classify_generation_failure(attempts: Sequence[Any]) -> str:
    """Classify whether a failed run is likely due to missing business intent."""
    combined_errors = " ".join(
        error.lower()
        for attempt in attempts
        for error in ([attempt.parse_error] if attempt.parse_error else [])
        + attempt.validation_errors
    )
    if not combined_errors:
        return "unknown"
    if any(keyword in combined_errors for keyword in _STRUCTURAL_ERROR_KEYWORDS):
        return "structural"
    if any(keyword in combined_errors for keyword in _AMBIGUITY_ERROR_KEYWORDS):
        return "ambiguous_intent"
    return "structural"


def _build_default_readme(
    *,
    contract: Mapping[str, Any],
    context: Mapping[str, Any],
    discovery_report: Any,
    resolve_provider_from_contract_fn: Callable[[Mapping[str, Any]], tuple[Optional[str], Any]],
    get_builds_fn: Callable[[Mapping[str, Any]], List[Mapping[str, Any]]],
) -> str:
    name = contract.get("name") or context.get("project_goal") or "FLUID Data Product"
    provider, _location = resolve_provider_from_contract_fn(dict(contract))
    lines = [
        f"# {name}",
        "",
        "AI-generated FLUID project scaffold.",
        "",
        "## Summary",
        "",
        f"- Provider: {provider or 'unknown'}",
        f"- Domain: {contract.get('domain', 'analytics')}",
        f"- Builds: {len(get_builds_fn(dict(contract)))}",
        f"- Discovered sources: {len(discovery_report.detected_sources)}",
        "",
        "## Quick Start",
        "",
        "```bash",
        "fluid validate contract.fluid.yaml",
        "fluid plan contract.fluid.yaml --provider local --out runtime/plan.json",
        "```",
        "",
        "## Notes",
        "",
        "This README was generated from metadata-only discovery. Review placeholder provider values before deployment.",
        "",
    ]
    return "\n".join(lines)


def _harmonise_agent_policy_inplace(contract: Dict[str, Any]) -> None:
    """Resolve the ``canReason=false but reasoning in allowedUseCases`` contradiction.

    LLMs occasionally emit policy blocks where ``canReason=false`` (set
    by the seed for confidential/restricted data) coexists with
    ``allowedUseCases: [reasoning, ...]``. The validator surfaces this
    as a warning, but a contract that ships with the contradiction
    embedded is genuinely incoherent. Strip ``reasoning`` from
    ``allowedUseCases`` whenever ``canReason=false`` so the contract
    self-validates without operator intervention.
    """
    exposes = contract.get("exposes") or []
    if not isinstance(exposes, list):
        return
    for expose in exposes:
        if not isinstance(expose, dict):
            continue
        policy = expose.get("policy")
        if not isinstance(policy, dict):
            continue
        agent_policy = policy.get("agentPolicy")
        if not isinstance(agent_policy, dict):
            continue
        # Only act when both fields are explicitly present + contradictory.
        if agent_policy.get("canReason") is False:
            allowed = agent_policy.get("allowedUseCases")
            if isinstance(allowed, list) and "reasoning" in allowed:
                agent_policy["allowedUseCases"] = [u for u in allowed if u != "reasoning"]


def normalize_generation_payload(
    payload: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    discovery_report: Any,
    seed_template: str,
    seed_provider: str,
    resolve_provider_from_contract_fn: Callable[[Mapping[str, Any]], tuple[Optional[str], Any]],
    get_builds_fn: Callable[[Mapping[str, Any]], List[Mapping[str, Any]]],
) -> Dict[str, Any]:
    """Normalize flexible LLM output into the shape the copilot flow expects."""
    contract = payload.get("contract")
    if not isinstance(contract, dict):
        contract_yaml = payload.get("contract_yaml")
        if isinstance(contract_yaml, str):
            contract = load_yaml_safe(contract_yaml)
    if not isinstance(contract, dict):
        raise ValueError("The LLM response did not include a valid contract object.")

    # Auto-resolve the canReason ↔ allowedUseCases:[reasoning] contradiction
    # before any downstream validator sees it. Cheap, deterministic, and
    # honest about the seed's intent (canReason=false means no reasoning).
    _harmonise_agent_policy_inplace(contract)

    readme_markdown = payload.get("readme_markdown")
    if not isinstance(readme_markdown, str) or not readme_markdown.strip():
        readme_markdown = _build_default_readme(
            contract=contract,
            context=context,
            discovery_report=discovery_report,
            resolve_provider_from_contract_fn=resolve_provider_from_contract_fn,
            get_builds_fn=get_builds_fn,
        )

    recommended_provider = normalize_provider_name(
        payload.get("recommended_provider")
        or resolve_provider_from_contract_fn(contract)[0]
        or seed_provider
    )
    recommended_template = normalize_template_name(
        payload.get("recommended_template") or seed_template
    )
    additional_files = sanitize_additional_files(payload.get("additional_files"))

    suggestions = {
        "recommended_template": recommended_template,
        "recommended_provider": recommended_provider,
        "recommended_patterns": list(payload.get("recommended_patterns") or []),
        "architecture_suggestions": list(payload.get("architecture_suggestions") or []),
        "best_practices": list(payload.get("best_practices") or []),
        "technology_stack": list(payload.get("technology_stack") or []),
        "canonical_model": _normalize_interview_summary(context).get("canonical_model"),
        "supporting_standards": _normalize_interview_summary(context).get("supporting_standards")
        or [],
        "description": payload.get("description") or contract.get("description") or "",
        "domain": payload.get("domain")
        or contract.get("domain")
        or _normalize_interview_summary(context).get("domain")
        or context.get("domain")
        or "",
        "owner": payload.get("owner")
        or contract.get("metadata", {}).get("owner", {}).get("team")
        or _normalize_interview_summary(context).get("owner_team"),
        "discovery_summary": {
            "provider_hints": discovery_report.provider_hints,
            "source_count": len(discovery_report.detected_sources),
        },
    }

    return {
        "contract": contract,
        "readme_markdown": readme_markdown,
        "additional_files": additional_files,
        "suggestions": suggestions,
    }


def validate_generated_result(
    normalized: Mapping[str, Any],
    *,
    capabilities: Mapping[str, Any],
    logger: Any,
    schema_manager_cls: Any,
    resolve_provider_from_contract_fn: Callable[[Mapping[str, Any]], tuple[Optional[str], Any]],
    get_builds_fn: Callable[[Mapping[str, Any]], List[Mapping[str, Any]]],
    context: Optional[Mapping[str, Any]] = None,
) -> tuple[List[str], List[str]]:
    """Validate contract schema plus local provider/build sanity checks.

    ``context`` is optional so pre-existing callers keep working; when
    supplied it drives additional technique-aware checks (e.g. DV2 runs
    must ship at least one ``hub_`` / ``sat_`` model in additional_files).
    """
    contract = normalized["contract"]
    suggestions = normalized["suggestions"]
    errors: List[str] = []
    warnings: List[str] = []

    schema_manager = schema_manager_cls(logger=logger)
    validation = schema_manager.validate_contract(contract, strict=True, offline_only=True)
    errors.extend(validation.errors)
    warnings.extend(validation.warnings)

    providers = set(capabilities.get("providers") or [])
    templates = set((capabilities.get("templates") or {}).keys())

    recommended_provider = suggestions.get("recommended_provider")
    if recommended_provider not in providers:
        errors.append(
            f"Unsupported provider '{recommended_provider}'. Use one of {sorted(providers)}"
        )

    recommended_template = suggestions.get("recommended_template")
    if recommended_template not in templates:
        errors.append(
            f"Unsupported template '{recommended_template}'. Use one of {sorted(templates)}"
        )

    contract_provider, _location = resolve_provider_from_contract_fn(contract)
    if not contract_provider:
        errors.append("Contract must expose at least one provider binding or runtime platform.")
    elif recommended_provider and contract_provider != recommended_provider:
        errors.append(
            f"Recommended provider '{recommended_provider}' does not match contract provider '{contract_provider}'."
        )

    builds = get_builds_fn(contract)
    if not builds:
        errors.append("Contract must include a build or builds section.")
    for index, build in enumerate(builds):
        build_id = build.get("id") or f"build_{index}"
        engine = str(
            build.get("engine") or build.get("transformation", {}).get("engine") or ""
        ).strip()
        if not engine:
            errors.append(f"Build '{build_id}' is missing an engine.")
            continue
        if engine not in KNOWN_BUILD_ENGINES:
            errors.append(f"Build '{build_id}' uses unsupported engine '{engine}'.")
            continue
        if contract_provider:
            supported = PROVIDER_ENGINE_COMPATIBILITY.get(contract_provider, set())
            if supported and engine not in supported:
                errors.append(
                    f"Build '{build_id}' uses engine '{engine}' which is not supported for provider '{contract_provider}'."
                )
        if engine == "python":
            repository = build.get("repository")
            model = (build.get("properties") or {}).get("model")
            if not repository or not model:
                errors.append(
                    f"Python build '{build_id}' must define repository and properties.model for execute compatibility."
                )
        if engine == "sql":
            properties = build.get("properties") or {}
            sql_text = properties.get("sql") or properties.get("sql_statements") or build.get("sql")
            if not sql_text:
                errors.append(
                    f"SQL build '{build_id}' must include SQL in build.properties.sql or sql_statements."
                )

    exposes = contract.get("exposes") or []
    if not exposes:
        errors.append("Contract must include at least one expose.")
    for expose in exposes:
        if not expose.get("exposeId"):
            errors.append("Each expose must include exposeId.")
        binding = expose.get("binding") or {}
        if not binding.get("platform"):
            errors.append(
                f"Expose '{expose.get('exposeId', 'unknown')}' is missing binding.platform."
            )
        contract_section = expose.get("contract") or {}
        if not contract_section.get("schema"):
            warnings.append(
                f"Expose '{expose.get('exposeId', 'unknown')}' does not define contract.schema."
            )
        semantics = expose.get("semantics")
        if not semantics:
            errors.append(f"Expose '{expose.get('exposeId', 'unknown')}' is missing semantics.")
            continue
        if not (semantics.get("entities") or []):
            errors.append(
                f"Expose '{expose.get('exposeId', 'unknown')}' semantics must include at least one entity."
            )
        if not (semantics.get("measures") or []):
            errors.append(
                f"Expose '{expose.get('exposeId', 'unknown')}' semantics must include at least one measure."
            )
        if not (semantics.get("dimensions") or []):
            errors.append(
                f"Expose '{expose.get('exposeId', 'unknown')}' semantics must include at least one dimension."
            )
        if not (semantics.get("metrics") or []):
            errors.append(
                f"Expose '{expose.get('exposeId', 'unknown')}' semantics must include at least one metric."
            )

    # --- Modeling-technique post-check (DV2 / dimensional) ---------------
    # Only fires when the LLM actually shipped additional_files but failed
    # to use the technique-appropriate naming.  When additional_files is
    # empty the engine's fallback skeleton handles the shape — don't burn
    # a repair attempt on an orthogonal issue.
    technique = (context or {}).get("data_modeling_technique") if context else None
    if technique in {"data_vault_2", "dimensional"}:
        additional_files = normalized.get("additional_files") or {}
        sql_file_names = [
            str(path).rsplit("/", 1)[-1]
            for path in additional_files
            if isinstance(path, str) and path.endswith(".sql")
        ]
        if sql_file_names:
            if technique == "data_vault_2" and not any(
                n.startswith(("hub_", "sat_", "lnk_")) for n in sql_file_names
            ):
                errors.append(
                    "data_modeling_technique=data_vault_2 requires at least one "
                    "hub_/sat_/lnk_ model in additional_files — none found."
                )
            if technique == "dimensional" and not any(
                n.startswith(("fct_", "dim_")) for n in sql_file_names
            ):
                errors.append(
                    "data_modeling_technique=dimensional requires at least one "
                    "fct_/dim_ model in additional_files — none found."
                )

    # --- Engine-owned file intrusion check -------------------------------
    # The engine owns dbt_project/models/sources.yml (generated from
    # upstream contracts) — duplicating the `sources:` block in any
    # LLM-shipped YAML makes dbt blow up with "two sources with the
    # same name". Catch this during validation so the repair loop can
    # fix it before apply runs.
    additional_files = normalized.get("additional_files") or {}
    for rel_path, content in additional_files.items():
        if not isinstance(rel_path, str) or not isinstance(content, str):
            continue
        if not rel_path.endswith(".yml") and not rel_path.endswith(".yaml"):
            continue
        # Only check YAML under dbt_project/models/... — e.g.
        # dbt_project/models/schema.yml. Other yamls (docs, CI configs)
        # are out of scope.
        if "/models/" not in rel_path:
            continue
        # Cheap regex-less scan; YAML parsing would be stricter but adds
        # a dependency on the LLM emitting syntactically clean YAML,
        # which is a separate concern.
        stripped_lines = [line.lstrip() for line in content.splitlines() if line.strip()]
        for line in stripped_lines[:20]:
            if line.startswith("sources:"):
                errors.append(
                    f"LLM-shipped {rel_path!r} declares `sources:` — that key is "
                    f"reserved for the engine-generated models/sources.yml. "
                    f"Remove the `sources:` block; keep only `models:`."
                )
                break

    return errors, warnings


def extract_json_object(text: str) -> Dict[str, Any]:
    """Extract a JSON object from a provider response."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped)
        stripped = re.sub(r"```$", "", stripped).strip()

    for candidate in (stripped, _slice_first_json_object(stripped)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Response did not contain a valid JSON object.")


def sanitize_additional_files(value: Any) -> Dict[str, str]:
    """Accept a bounded set of relative text files proposed by the model."""
    if not isinstance(value, Mapping):
        return {}
    sanitized: Dict[str, str] = {}
    for raw_path, raw_content in value.items():
        if not isinstance(raw_path, str) or not isinstance(raw_content, str):
            continue
        normalized_raw_path = raw_path.replace("\\", "/")
        is_absolute_like = normalized_raw_path.startswith("/") or bool(
            re.match(r"^[a-zA-Z]:/", normalized_raw_path)
        )
        candidate = Path(normalized_raw_path)
        if is_absolute_like or candidate.is_absolute() or ".." in candidate.parts:
            continue
        if candidate.suffix.lower() not in SAFE_ADDITIONAL_FILE_EXTENSIONS:
            continue
        sanitized[candidate.as_posix()] = raw_content
    return sanitized


def _slice_first_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
