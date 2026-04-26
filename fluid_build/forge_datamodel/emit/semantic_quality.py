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

"""Semantic quality lint for forged logical data models.

These checks sit above schema validation. Pydantic can prove a draft is
well-shaped JSON; these rules prove the model carries enough business
meaning to be useful, reviewable, and safe to generate from.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, List

from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DV2Model,
)
from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft, ValidationFinding

_PLACEHOLDER_RE = re.compile(
    r"(^|_)(todo|tbd|dummy|placeholder|unknown|example|sample)(_|$)",
    re.IGNORECASE,
)
_ALLOWED_TIME_GRAINS = {"day", "week", "month", "quarter", "year", "hour", "minute"}
_TIME_GRAIN_ALIASES = {
    "days": "day",
    "daily": "day",
    "weeks": "week",
    "weekly": "week",
    "months": "month",
    "monthly": "month",
    "quarters": "quarter",
    "quarterly": "quarter",
    "years": "year",
    "yearly": "year",
    "hours": "hour",
    "hourly": "hour",
    "minutes": "minute",
    "minutely": "minute",
    "s": "minute",
    "ms": "minute",
    "sec": "minute",
    "secs": "minute",
    "second": "minute",
    "seconds": "minute",
    "millisecond": "minute",
    "milliseconds": "minute",
}


def lint_logical_semantic_quality(logical: LogicalDraft) -> List[ValidationFinding]:
    """Return quality findings for a forged logical draft.

    Error-level findings are reserved for problems that make the output unsafe
    as a source of truth: empty technique branches, missing DV2 business keys,
    orphan relationships, or invalid OSI time grains. Advisory modeling gaps,
    such as missing fact measures, remain warnings so existing contracts can be
    reviewed and improved without surprising users with unnecessary hard stops.
    """
    findings: list[ValidationFinding] = []
    findings.extend(_lint_common(logical))

    if logical.technique == "data_vault_2" and logical.dv2 is not None:
        findings.extend(_lint_dv2(logical.dv2))
    elif logical.technique == "dimensional" and logical.dimensional is not None:
        findings.extend(_lint_dimensional(logical.dimensional))

    findings.extend(_lint_osi(logical.osi))
    return findings


def _lint_common(logical: LogicalDraft) -> List[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field, value in (("name", logical.name), ("description", logical.description)):
        if field == "description" and not value:
            continue
        if _looks_placeholder(value):
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=field,
                    message=(
                        f"Logical model {field} '{value}' looks placeholder-like. "
                        "Use domain language that a reviewer can map to the business process."
                    ),
                )
            )
    return findings


def _lint_dv2(model: DV2Model) -> List[ValidationFinding]:
    findings: list[ValidationFinding] = []
    hub_names = [hub.hub_table_name for hub in model.hubs]
    hub_name_set = set(hub_names)

    if not model.hubs:
        findings.append(
            ValidationFinding(
                severity="error",
                field="dv2.hubs",
                message="Data Vault 2.0 models must contain at least one hub.",
            )
        )

    for name in _duplicates(hub_names):
        findings.append(
            ValidationFinding(
                severity="error",
                field="dv2.hubs",
                message=f"Duplicate DV2 hub table name '{name}' detected.",
            )
        )

    for index, hub in enumerate(model.hubs):
        field = f"dv2.hubs[{index}]"
        if not hub.business_key_columns:
            findings.append(
                ValidationFinding(
                    severity="error",
                    field=f"{field}.business_key_columns",
                    message=(
                        f"Hub '{hub.hub_table_name}' must declare business_key_columns. "
                        "A hub without business keys cannot be loaded or reconciled."
                    ),
                )
            )
        if _looks_placeholder(hub.entity_name) or _looks_placeholder(hub.hub_table_name):
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=field,
                    message=f"Hub '{hub.hub_table_name}' uses placeholder-like naming.",
                )
            )

    for index, link in enumerate(model.links):
        field = f"dv2.links[{index}]"
        if len(link.hubs_involved) < 2:
            findings.append(
                ValidationFinding(
                    severity="error",
                    field=f"{field}.hubs_involved",
                    message=(
                        f"Link '{link.link_table_name}' must connect at least two hubs; "
                        f"found {len(link.hubs_involved)}."
                    ),
                )
            )
        unknown_hubs = [hub for hub in link.hubs_involved if hub not in hub_name_set and hub_names]
        if unknown_hubs:
            findings.append(
                ValidationFinding(
                    severity="error",
                    field=f"{field}.hubs_involved",
                    message=(
                        f"Link '{link.link_table_name}' references unknown hub(s): "
                        f"{', '.join(sorted(unknown_hubs))}."
                    ),
                )
            )
        if not link.join_keys and not link.relationships:
            findings.append(
                ValidationFinding(
                    severity="info",
                    field=f"{field}.join_keys",
                    message=(
                        f"Link '{link.link_table_name}' has no join_keys or relationships. "
                        "This can be acceptable for lineage-only inference, but should be reviewed."
                    ),
                )
            )

    for index, satellite in enumerate(model.satellites):
        field = f"dv2.satellites[{index}]"
        if hub_name_set and satellite.parent_hub not in hub_name_set:
            findings.append(
                ValidationFinding(
                    severity="error",
                    field=f"{field}.parent_hub",
                    message=(
                        f"Satellite '{satellite.satellite_table_name}' references unknown "
                        f"parent_hub '{satellite.parent_hub}'."
                    ),
                )
            )
        if not satellite.attributes:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=f"{field}.attributes",
                    message=(
                        f"Satellite '{satellite.satellite_table_name}' has no descriptive "
                        "attributes, so it will not add analytic value beyond the hub key."
                    ),
                )
            )

    return findings


def _lint_dimensional(model: DimensionalModel) -> List[ValidationFinding]:
    findings: list[ValidationFinding] = []
    fact_names = [fact.name for fact in model.facts]
    dim_names = [dimension.name for dimension in model.dimensions]
    dim_name_set = set(dim_names)

    if not model.facts:
        findings.append(
            ValidationFinding(
                severity="error",
                field="dimensional.facts",
                message="Dimensional models must contain at least one fact table.",
            )
        )

    for name in _duplicates(fact_names):
        findings.append(
            ValidationFinding(
                severity="error",
                field="dimensional.facts",
                message=f"Duplicate fact table name '{name}' detected.",
            )
        )
    for name in _duplicates(dim_names):
        findings.append(
            ValidationFinding(
                severity="error",
                field="dimensional.dimensions",
                message=f"Duplicate dimension table name '{name}' detected.",
            )
        )

    for index, fact in enumerate(model.facts):
        field = f"dimensional.facts[{index}]"
        if not fact.grain_statement.strip():
            findings.append(
                ValidationFinding(
                    severity="error",
                    field=f"{field}.grain_statement",
                    message=f"Fact '{fact.name}' must state its grain.",
                )
            )
        if not fact.measures:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=f"{field}.measures",
                    message=(
                        f"Fact '{fact.name}' has no measures. A fact table should expose "
                        "at least one additive, semi-additive, or countable measure."
                    ),
                )
            )
        if model.dimensions and not fact.foreign_keys:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=f"{field}.foreign_keys",
                    message=(
                        f"Fact '{fact.name}' has dimensions but no foreign_keys. "
                        "Generated joins may be incomplete."
                    ),
                )
            )
        unknown_fk_dims = [
            key
            for key in fact.foreign_keys
            if _dimension_name_from_fk(key) not in dim_name_set and dim_name_set
        ]
        if unknown_fk_dims:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=f"{field}.foreign_keys",
                    message=(
                        f"Fact '{fact.name}' has foreign_keys that do not map cleanly "
                        f"to emitted dimensions: {', '.join(sorted(unknown_fk_dims))}."
                    ),
                )
            )
        if _looks_placeholder(fact.name):
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=field,
                    message=f"Fact '{fact.name}' uses placeholder-like naming.",
                )
            )

    for index, dimension in enumerate(model.dimensions):
        field = f"dimensional.dimensions[{index}]"
        if not dimension.attributes:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=f"{field}.attributes",
                    message=(
                        f"Dimension '{dimension.name}' has no attributes, so it will "
                        "not be useful for slicing, filtering, or BI display."
                    ),
                )
            )
        if not dimension.surrogate_key and not dimension.natural_keys:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=field,
                    message=(
                        f"Dimension '{dimension.name}' should declare a surrogate_key "
                        "or natural_keys for deterministic joins."
                    ),
                )
            )
        if _looks_placeholder(dimension.name):
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field=field,
                    message=f"Dimension '{dimension.name}' uses placeholder-like naming.",
                )
            )

    return findings


def _lint_osi(model: OSISemanticModel) -> List[ValidationFinding]:
    findings: list[ValidationFinding] = []

    dataset_names = [dataset.name for dataset in model.datasets]
    metric_names = [metric.name for metric in model.metrics]
    for name in _duplicates(dataset_names):
        findings.append(
            ValidationFinding(
                severity="error",
                field="osi.datasets",
                message=f"Duplicate OSI dataset name '{name}' detected.",
            )
        )
    for name in _duplicates(metric_names):
        findings.append(
            ValidationFinding(
                severity="error",
                field="osi.metrics",
                message=f"Duplicate OSI metric name '{name}' detected.",
            )
        )

    for dataset_index, dataset in enumerate(model.datasets):
        seen_fields: set[str] = set()
        for field_index, field in enumerate(dataset.fields):
            field_path = f"osi.datasets[{dataset_index}].fields[{field_index}]"
            if field.name in seen_fields:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        field=field_path,
                        message=(f"Dataset '{dataset.name}' repeats field name '{field.name}'."),
                    )
                )
            seen_fields.add(field.name)
            grain = field.dimension.grain if field.dimension is not None else None
            normalized_grain = _normalize_time_grain(grain) if grain is not None else None
            if grain is not None and normalized_grain not in _ALLOWED_TIME_GRAINS:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        field=f"{field_path}.dimension.grain",
                        message=(
                            f"Time dimension '{field.name}' uses unsupported grain "
                            f"'{grain}'. Use one of: {', '.join(sorted(_ALLOWED_TIME_GRAINS))}."
                        ),
                    )
                )

    for metric_index, metric in enumerate(model.metrics):
        dialects = getattr(metric.expression, "dialects", []) or []
        if not any(str(getattr(dialect, "expression", "")).strip() for dialect in dialects):
            findings.append(
                ValidationFinding(
                    severity="error",
                    field=f"osi.metrics[{metric_index}].expression",
                    message=f"Metric '{metric.name}' must include a non-empty expression.",
                )
            )

    return findings


def _normalize_time_grain(value: str) -> str:
    normalized = (value or "").strip().lower().replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.removeprefix("per ").removesuffix(" grain").strip()
    return _TIME_GRAIN_ALIASES.get(normalized, normalized)


def _duplicates(values: Iterable[str]) -> dict[str, int]:
    return {name: count for name, count in Counter(values).items() if count > 1}


def _looks_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.search(value.strip()))


def _dimension_name_from_fk(key: str) -> str:
    normalized = key.strip().lower()
    if normalized.startswith("dim_"):
        return normalized
    if normalized.endswith("_key"):
        normalized = normalized[: -len("_key")]
    if normalized.endswith("_id"):
        normalized = normalized[: -len("_id")]
    return f"dim_{normalized}"


__all__ = ["lint_logical_semantic_quality"]
