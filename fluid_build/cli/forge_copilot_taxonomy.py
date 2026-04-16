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

"""Shared copilot taxonomy and normalization helpers."""

from __future__ import annotations

__all__ = [
    "CANONICAL_MODEL_LABELS",
    "SUPPORTING_STANDARD_LABELS",
    "USE_CASE_CHOICES",
    "USE_CASE_LABELS",
    "CI_PROVIDER_VALUES",
    "CI_COMPLEXITY_VALUES",
    "clean_text",
    "canonicalize_use_case_text",
    "infer_modeling_context",
    "normalize_canonical_model",
    "normalize_ci_complexity",
    "normalize_ci_provider",
    "normalize_use_case",
    "format_use_case_label",
    "normalize_copilot_context",
    "normalize_supporting_standards",
]

import re
from typing import Any, Dict, List, Optional

USE_CASE_CHOICES: List[Dict[str, str]] = [
    {"label": "Analytics & BI", "value": "analytics"},
    {"label": "ETL / Data Pipelines", "value": "etl_pipeline"},
    {"label": "Streaming / Real-time", "value": "streaming"},
    {"label": "ML / Feature Engineering", "value": "ml_pipeline"},
    {"label": "Data Platform / Lakehouse", "value": "data_platform"},
    {"label": "Other / Not sure", "value": "other"},
]

USE_CASE_LABELS = {choice["value"]: choice["label"] for choice in USE_CASE_CHOICES}

CANONICAL_MODEL_LABELS = {
    "tmf_sid": "TM Forum SID",
    "nrf_arts": "NRF ARTS",
    "gs1_gdm": "GS1 Global Data Model",
    "adobe_xdm": "Adobe XDM",
    "hl7_fhir": "HL7 FHIR",
    "omop_cdm": "OMOP CDM",
}

SUPPORTING_STANDARD_LABELS = {
    "gs1_gdm": "GS1 Global Data Model",
    "gs1_epcis_cbv": "GS1 EPCIS / CBV",
}

USE_CASE_ALIASES = {
    "analytics": "analytics",
    "analytics and bi": "analytics",
    "analytics bi": "analytics",
    "business intelligence": "analytics",
    "bi": "analytics",
    "reporting": "analytics",
    "dashboard": "analytics",
    "dashboards": "analytics",
    "etl": "etl_pipeline",
    "etl pipeline": "etl_pipeline",
    "etl data pipelines": "etl_pipeline",
    "data pipeline": "etl_pipeline",
    "data pipelines": "etl_pipeline",
    "pipeline": "etl_pipeline",
    "pipelines": "etl_pipeline",
    "streaming": "streaming",
    "streaming real time": "streaming",
    "real time": "streaming",
    "realtime": "streaming",
    "real time analytics": "streaming",
    "ml": "ml_pipeline",
    "ml pipeline": "ml_pipeline",
    "ml feature engineering": "ml_pipeline",
    "machine learning": "ml_pipeline",
    "machine learning pipeline": "ml_pipeline",
    "machine learning model": "ml_pipeline",
    "feature engineering": "ml_pipeline",
    "data lake": "data_platform",
    "data lakes": "data_platform",
    "data lakehouse": "data_platform",
    "data platform": "data_platform",
    "data platform lakehouse": "data_platform",
    "lakehouse": "data_platform",
    "other": "other",
    "other not sure": "other",
    "not sure": "other",
    "not certain": "other",
    "unsure": "other",
}

CANONICAL_MODEL_ALIASES = {
    "tmf sid": "tmf_sid",
    "tmf_sid": "tmf_sid",
    "tm forum sid": "tmf_sid",
    "sid": "tmf_sid",
    "nrf arts": "nrf_arts",
    "nrf_arts": "nrf_arts",
    "arts": "nrf_arts",
    "retail operational data model": "nrf_arts",
    "gs1 gdm": "gs1_gdm",
    "gs1_gdm": "gs1_gdm",
    "gs1 global data model": "gs1_gdm",
    "adobe xdm": "adobe_xdm",
    "adobe_xdm": "adobe_xdm",
    "xdm": "adobe_xdm",
    "experience data model": "adobe_xdm",
    "hl7 fhir": "hl7_fhir",
    "hl7_fhir": "hl7_fhir",
    "fhir": "hl7_fhir",
    "omop": "omop_cdm",
    "omop cdm": "omop_cdm",
    "omop_cdm": "omop_cdm",
}

SUPPORTING_STANDARD_ALIASES = {
    "gs1 gdm": "gs1_gdm",
    "gs1_gdm": "gs1_gdm",
    "gs1 global data model": "gs1_gdm",
    "epcis": "gs1_epcis_cbv",
    "cbv": "gs1_epcis_cbv",
    "gs1 epcis": "gs1_epcis_cbv",
    "gs1 cbv": "gs1_epcis_cbv",
    "gs1 epcis cbv": "gs1_epcis_cbv",
    "gs1_epcis_cbv": "gs1_epcis_cbv",
}


# CI/CD providers — must stay in sync with PipelineProvider in
# ``fluid_build/forge/core/pipeline_templates.py``. Inlined here to avoid
# importing a ``forge.core`` module from the ``cli`` taxonomy layer.
CI_PROVIDER_VALUES = frozenset(
    {
        "github_actions",
        "gitlab_ci",
        "azure_devops",
        "jenkins",
        "bitbucket",
        "circle_ci",
        "tekton",
    }
)

CI_COMPLEXITY_VALUES = frozenset({"basic", "standard", "advanced", "enterprise"})

CI_PROVIDER_ALIASES = {
    "gh": "github_actions",
    "gha": "github_actions",
    "github": "github_actions",
    "github actions": "github_actions",
    "github_actions": "github_actions",
    "ghactions": "github_actions",
    "gl": "gitlab_ci",
    "gitlab": "gitlab_ci",
    "gitlab ci": "gitlab_ci",
    "gitlab_ci": "gitlab_ci",
    "gitlabci": "gitlab_ci",
    "azure": "azure_devops",
    "azdo": "azure_devops",
    "ado": "azure_devops",
    "azure devops": "azure_devops",
    "azure_devops": "azure_devops",
    "azurepipelines": "azure_devops",
    "azure pipelines": "azure_devops",
    "jenkins": "jenkins",
    "jenkinsfile": "jenkins",
    "bb": "bitbucket",
    "bitbucket": "bitbucket",
    "bitbucket pipelines": "bitbucket",
    "bitbucket_pipelines": "bitbucket",
    "circle": "circle_ci",
    "circleci": "circle_ci",
    "circle ci": "circle_ci",
    "circle_ci": "circle_ci",
    "tekton": "tekton",
    "tkn": "tekton",
}


def clean_text(value: Any) -> str:
    """Return a trimmed string for optional context values."""
    return str(value or "").strip()


def canonicalize_use_case_text(value: Any) -> str:
    """Return a comparison-friendly use-case string."""
    text = clean_text(value).lower()
    if not text:
        return ""
    text = text.replace("&", " and ")
    text = re.sub(r"[_/\\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_use_case(value: Any) -> Optional[str]:
    """Normalize use-case variants to stable internal values."""
    text = canonicalize_use_case_text(value)
    if not text:
        return None
    if text in USE_CASE_ALIASES:
        return USE_CASE_ALIASES[text]
    if "machine learning" in text or "feature engineering" in text or re.search(r"\bml\b", text):
        return "ml_pipeline"
    if "streaming" in text or "real time" in text or "realtime" in text:
        return "streaming"
    if "data platform" in text or "data lake" in text or "lakehouse" in text:
        return "data_platform"
    if (
        "etl" in text
        or "data pipeline" in text
        or "pipeline" in text
        or "cdc" in text
        or "sync" in text
    ):
        return "etl_pipeline"
    if (
        "analytics" in text
        or "reporting" in text
        or "dashboard" in text
        or "scorecard" in text
        or "business intelligence" in text
        or re.search(r"\bbi\b", text)
    ):
        return "analytics"
    if "other" in text or "not sure" in text or "unsure" in text:
        return "other"
    return None


def format_use_case_label(use_case: Any, use_case_other: Any = None) -> str:
    """Return the user-facing use-case label for display surfaces."""
    other_text = clean_text(use_case_other)
    canonical = normalize_use_case(use_case)
    if canonical == "other" and other_text:
        return other_text
    if canonical:
        return USE_CASE_LABELS.get(canonical, canonical.replace("_", " ").title())
    if other_text:
        return other_text
    raw = clean_text(use_case)
    return raw or USE_CASE_LABELS["analytics"]


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def _listify_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = clean_text(value).replace("\n", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def normalize_canonical_model(value: Any) -> Optional[str]:
    """Normalize canonical-model variants to stable internal values."""
    text = canonicalize_use_case_text(value)
    if not text:
        return None
    return CANONICAL_MODEL_ALIASES.get(text)


def normalize_supporting_standards(value: Any) -> list[str]:
    """Normalize supporting standards into a stable ordered list."""
    normalized: list[str] = []
    for item in _listify_strings(value):
        standard = SUPPORTING_STANDARD_ALIASES.get(canonicalize_use_case_text(item))
        if standard:
            normalized.append(standard)
    return _dedupe_preserve_order(normalized)


def _combined_intent_text(context: Dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "domain",
        "project_goal",
        "data_sources",
        "use_case",
        "use_case_other",
        "primary_entity",
        "time_dimension",
    ):
        value = clean_text(context.get(key))
        if value:
            parts.append(value)
    for key in ("primary_measures", "primary_dimensions", "supporting_standards"):
        parts.extend(_listify_strings(context.get(key)))
    return canonicalize_use_case_text(" ".join(parts))


def _looks_telco(text: str, domain: str) -> bool:
    return domain == "telco" or any(
        token in text
        for token in (
            "telco",
            "telecom",
            "subscriber",
            "oss",
            "bss",
            "tm forum",
            "tmf",
            "sid",
            "service assurance",
        )
    )


def _looks_retail(text: str, domain: str) -> bool:
    return domain == "retail" or any(
        token in text
        for token in (
            "retail",
            "commerce",
            "omnichannel",
            "basket",
            "inventory",
            "store",
            "sku",
            "customer 360",
            "recommendation",
            "traceability",
        )
    )


def _looks_healthcare(text: str, domain: str) -> bool:
    return domain == "healthcare" or any(
        token in text
        for token in (
            "healthcare",
            "clinical",
            "patient",
            "ehr",
            "emr",
            "fhir",
            "hipaa",
            "claims",
            "population health",
            "omop",
        )
    )


def infer_modeling_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Infer canonical modeling guidance from domain and user intent."""
    domain = clean_text(context.get("domain")).lower()
    text = _combined_intent_text(context)
    use_case = normalize_use_case(context.get("use_case")) or ""

    canonical_model = normalize_canonical_model(context.get("canonical_model"))
    supporting_standards = normalize_supporting_standards(context.get("supporting_standards"))

    digital_retail = any(
        token in text
        for token in (
            "adobe",
            "xdm",
            "digital experience",
            "experience event",
            "web sdk",
            "profile",
            "journey",
        )
    )
    retail_traceability = any(
        token in text
        for token in (
            "traceability",
            "epcis",
            "cbv",
            "chain of custody",
            "lot",
            "serial",
            "trade item",
            "supply chain",
        )
    )
    healthcare_interop = any(
        token in text
        for token in (
            "fhir",
            "hl7",
            "ehr",
            "emr",
            "interoperability",
            "clinical integration",
            "care coordination",
        )
    )
    healthcare_analytics = any(
        token in text
        for token in (
            "omop",
            "research",
            "population health",
            "cohort",
            "claims",
            "secondary use",
            "observational",
        )
    )

    if canonical_model is None:
        if digital_retail:
            canonical_model = "adobe_xdm"
        elif retail_traceability:
            canonical_model = "gs1_gdm"
        elif _looks_telco(text, domain):
            canonical_model = "tmf_sid"
        elif _looks_healthcare(text, domain):
            if healthcare_interop:
                canonical_model = "hl7_fhir"
            elif healthcare_analytics or use_case in {"analytics", "ml_pipeline", "data_platform"}:
                canonical_model = "omop_cdm"
            else:
                canonical_model = (
                    "hl7_fhir" if use_case in {"etl_pipeline", "streaming"} else "omop_cdm"
                )
        elif _looks_retail(text, domain):
            canonical_model = "nrf_arts"

    if canonical_model == "nrf_arts" and _looks_retail(text, domain):
        supporting_standards.append("gs1_gdm")
    if canonical_model == "gs1_gdm" or retail_traceability:
        supporting_standards.extend(["gs1_gdm", "gs1_epcis_cbv"])

    return {
        "canonical_model": canonical_model,
        "supporting_standards": _dedupe_preserve_order(supporting_standards),
    }


def normalize_ci_provider(value: Any) -> Optional[str]:
    """Canonicalize a CI provider string into a ``PipelineProvider`` value.

    Returns ``None`` when the value is empty or unrecognized.
    """
    text = clean_text(value)
    if not text:
        return None
    key = text.lower().replace("-", " ").replace("_", " ")
    key = re.sub(r"\s+", " ", key).strip()
    # Try the compound alias first, then collapse spaces for canonical lookup.
    if key in CI_PROVIDER_ALIASES:
        return CI_PROVIDER_ALIASES[key]
    collapsed = key.replace(" ", "_")
    if collapsed in CI_PROVIDER_ALIASES:
        return CI_PROVIDER_ALIASES[collapsed]
    if collapsed in CI_PROVIDER_VALUES:
        return collapsed
    return None


def normalize_ci_complexity(value: Any) -> Optional[str]:
    """Canonicalize a CI complexity string into a ``PipelineComplexity`` value."""
    text = clean_text(value)
    if not text:
        return None
    key = text.lower().strip()
    return key if key in CI_COMPLEXITY_VALUES else None


def normalize_copilot_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize known copilot context fields without dropping unknown keys."""
    normalized = dict(context)
    use_case = normalize_use_case(normalized.get("use_case"))
    if use_case:
        normalized["use_case"] = use_case
    other_text = clean_text(normalized.get("use_case_other"))
    if other_text:
        normalized["use_case_other"] = other_text
    else:
        normalized.pop("use_case_other", None)
    canonical_model = normalize_canonical_model(normalized.get("canonical_model"))
    if canonical_model:
        normalized["canonical_model"] = canonical_model
    else:
        normalized.pop("canonical_model", None)
    supporting_standards = normalize_supporting_standards(normalized.get("supporting_standards"))
    if supporting_standards:
        normalized["supporting_standards"] = supporting_standards
    else:
        normalized.pop("supporting_standards", None)

    inferred = infer_modeling_context(normalized)
    if inferred["canonical_model"] and not normalized.get("canonical_model"):
        normalized["canonical_model"] = inferred["canonical_model"]
    merged_supporting = _dedupe_preserve_order(
        normalize_supporting_standards(normalized.get("supporting_standards"))
        + list(inferred["supporting_standards"])
    )
    if merged_supporting:
        normalized["supporting_standards"] = merged_supporting
    else:
        normalized.pop("supporting_standards", None)

    # CI/CD pipeline preferences (auto-scaffold inside `fluid forge`).
    ci_provider = normalize_ci_provider(normalized.get("ci_provider"))
    if ci_provider:
        normalized["ci_provider"] = ci_provider
    else:
        normalized.pop("ci_provider", None)
    ci_complexity = normalize_ci_complexity(normalized.get("ci_complexity"))
    if ci_complexity:
        normalized["ci_complexity"] = ci_complexity
    else:
        normalized.pop("ci_complexity", None)
    return normalized
