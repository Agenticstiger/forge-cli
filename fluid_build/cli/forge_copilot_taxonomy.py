# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared copilot taxonomy and normalization helpers."""

from __future__ import annotations

__all__ = [
    "USE_CASE_CHOICES",
    "USE_CASE_LABELS",
    "CI_PROVIDER_VALUES",
    "CI_COMPLEXITY_VALUES",
    "clean_text",
    "canonicalize_use_case_text",
    "normalize_ci_complexity",
    "normalize_ci_provider",
    "normalize_use_case",
    "format_use_case_label",
    "normalize_copilot_context",
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
