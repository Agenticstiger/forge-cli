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

"""Emit a FLUID 0.7.2 contract from a logical draft."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.schema_manager import FluidSchemaManager


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return value or "data_model"


def _default_build(build_engine: str, slug: str) -> Dict[str, Any]:
    if build_engine == "sql":
        return {
            "id": "main",
            "engine": "sql",
            "pattern": "embedded-logic",
            "repository": "./sql_project",
            "properties": {"sql": "SELECT 1 AS placeholder"},
        }
    return {
        "id": "main",
        "engine": build_engine,
        "pattern": "hybrid-reference",
        "repository": "./dbt_project" if build_engine == "dbt" else "./models",
        "properties": {"model": "main"},
    }


def _expose_schema(logical: LogicalDraft) -> List[Dict[str, Any]]:
    if logical.osi.datasets:
        dataset = logical.osi.datasets[0]
        columns = []
        for field in dataset.fields:
            columns.append(
                {
                    "name": field.name,
                    "type": field.data_type or "STRING",
                    "required": field.name in dataset.primary_key,
                }
            )
        if columns:
            return columns
    return [{"name": "id", "type": "STRING", "required": True}]


def _first_expression(field_or_metric: Any) -> str:
    expression = getattr(field_or_metric, "expression", None)
    dialects = getattr(expression, "dialects", None) or []
    if dialects:
        return dialects[0].expression
    return getattr(field_or_metric, "name", "value")


def _is_time_name(name: str) -> bool:
    lower = name.lower()
    return any(token in lower for token in ("date", "time", "timestamp", "day"))


def _semantic_field_expr(name: str) -> str:
    return name or "value"


def _measure_agg(expr: str) -> str:
    upper = (expr or "").upper()
    if upper.startswith("COUNT("):
        return "count"
    if upper.startswith("AVG("):
        return "avg"
    if upper.startswith("MIN("):
        return "min"
    if upper.startswith("MAX("):
        return "max"
    return "sum"


_FLUID_TIME_GRAINS = {"day", "week", "month", "quarter", "year", "hour", "minute"}
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


def _fluid_time_grain(value: str) -> str | None:
    normalized = (value or "").strip().lower().replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.removeprefix("per ").removesuffix(" grain").strip()
    normalized = _TIME_GRAIN_ALIASES.get(normalized, normalized)
    return normalized if normalized in _FLUID_TIME_GRAINS else None


def _fluid_semantics(logical: LogicalDraft) -> Dict[str, Any]:
    semantics: Dict[str, Any] = {
        "name": logical.osi.name,
        "description": logical.osi.description or logical.description,
    }
    datasets = logical.osi.datasets or []
    first_dataset = datasets[0] if datasets else None
    entities: List[Dict[str, Any]] = []
    dimensions: List[Dict[str, Any]] = []
    measures: List[Dict[str, Any]] = []
    metrics: List[Dict[str, Any]] = []

    if first_dataset is not None:
        for primary_key in first_dataset.primary_key:
            entities.append(
                {
                    "name": primary_key.replace("_id", "") or primary_key,
                    "type": "primary",
                    "expr": primary_key,
                }
            )
        for relationship in logical.osi.relationships:
            expr = relationship.from_columns[0] if relationship.from_columns else relationship.name
            entities.append({"name": relationship.to, "type": "foreign", "expr": expr})
        for field in first_dataset.fields:
            if field.name in first_dataset.primary_key:
                continue
            dimension_type = "categorical"
            dimension_doc: Dict[str, Any] = {"name": field.name, "type": dimension_type}
            expr = _first_expression(field)
            if expr and expr != field.name:
                dimension_doc["expr"] = expr
            if field.dimension and field.dimension.is_time:
                dimension_doc["type"] = "time"
                if field.dimension.grain:
                    time_grain = _fluid_time_grain(field.dimension.grain)
                    if time_grain:
                        dimension_doc["typeParams"] = {
                            "timeGranularity": time_grain,
                        }
            dimensions.append(dimension_doc)

    for metric in logical.osi.metrics:
        expr = _first_expression(metric)
        measure = {
            "name": metric.name,
            "agg": _measure_agg(expr),
            "expr": expr,
        }
        if metric.description:
            measure["description"] = metric.description
        measures.append(measure)
        metrics.append(
            {
                "name": metric.name,
                "type": "simple",
                "measure": metric.name,
                "description": metric.description or metric.name,
            }
        )

    if logical.dimensional is not None:
        if not entities:
            for fact in logical.dimensional.facts:
                entities.append(
                    {
                        "name": fact.name,
                        "type": "primary",
                        "expr": f"{_slug(fact.name).removeprefix('fact_').removeprefix('fct_')}_id",
                    }
                )
            for dimension in logical.dimensional.dimensions:
                entities.append(
                    {
                        "name": dimension.name,
                        "type": "foreign",
                        "expr": (
                            dimension.natural_keys[0] if dimension.natural_keys else dimension.name
                        ),
                    }
                )
        if not dimensions:
            for fact in logical.dimensional.facts:
                for column in fact.foreign_keys + fact.degenerate_dimensions:
                    doc: Dict[str, Any] = {"name": column, "type": "categorical", "expr": column}
                    if _is_time_name(column):
                        doc["type"] = "time"
                        doc["typeParams"] = {"timeGranularity": "day"}
                    dimensions.append(doc)
            for dimension in logical.dimensional.dimensions:
                for attr in dimension.attributes:
                    doc = {"name": attr.name, "type": "categorical", "expr": attr.name}
                    if _is_time_name(attr.name):
                        doc["type"] = "time"
                        doc["typeParams"] = {"timeGranularity": "day"}
                    dimensions.append(doc)
            if not dimensions:
                for dimension in logical.dimensional.dimensions:
                    key = (
                        dimension.natural_keys[0]
                        if dimension.natural_keys
                        else dimension.surrogate_key or dimension.name
                    )
                    dimensions.append({"name": key, "type": "categorical", "expr": key})
                for fact in logical.dimensional.facts:
                    grain_key = f"{_slug(fact.name).removeprefix('fact_').removeprefix('fct_')}_id"
                    dimensions.append({"name": grain_key, "type": "categorical", "expr": grain_key})
        if not measures or not metrics:
            for fact in logical.dimensional.facts:
                for measure_field in fact.measures:
                    if not any(item.get("name") == measure_field.name for item in measures):
                        measures.append(
                            {
                                "name": measure_field.name,
                                "agg": "sum",
                                "expr": _semantic_field_expr(measure_field.name),
                                "description": measure_field.description
                                or f"Sum of {measure_field.name}.",
                            }
                        )
                    if not any(item.get("name") == measure_field.name for item in metrics):
                        metrics.append(
                            {
                                "name": measure_field.name,
                                "type": "simple",
                                "measure": measure_field.name,
                                "description": measure_field.description or measure_field.name,
                            }
                        )
            if not measures:
                measures.append({"name": "record_count", "agg": "count", "expr": "*"})
            if not metrics:
                metrics.append(
                    {
                        "name": "record_count",
                        "type": "simple",
                        "measure": "record_count",
                        "description": "Count of records.",
                    }
                )

    if logical.dv2 is not None:
        if not entities:
            for hub in logical.dv2.hubs:
                key = hub.business_key_columns[0] if hub.business_key_columns else hub.entity_name
                entities.append({"name": hub.entity_name, "type": "primary", "expr": key})
        if not dimensions:
            for satellite in logical.dv2.satellites:
                for attr in satellite.attributes:
                    doc = {"name": attr, "type": "categorical", "expr": attr}
                    if _is_time_name(attr):
                        doc["type"] = "time"
                        doc["typeParams"] = {"timeGranularity": "day"}
                    dimensions.append(doc)
            if not dimensions:
                for hub in logical.dv2.hubs:
                    for key in hub.business_key_columns or [hub.entity_name]:
                        dimensions.append({"name": key, "type": "categorical", "expr": key})
        if not measures:
            measures.append({"name": "record_count", "agg": "count", "expr": "*"})
        if not metrics:
            metrics.append(
                {
                    "name": "record_count",
                    "type": "simple",
                    "measure": "record_count",
                    "description": "Count of records.",
                }
            )

    if entities:
        semantics["entities"] = entities
    if dimensions:
        semantics["dimensions"] = dimensions
    if measures:
        semantics["measures"] = measures
    if metrics:
        semantics["metrics"] = metrics
    return semantics


def _build_provenance_block(annotations: Any) -> Optional[str]:
    """Item 7 — serialise an :class:`AnnotationLog` into a JSON
    string suitable for ``metadata.labels.provenance``.

    Fluid 0.7.x's ``metadata.labels`` is a flat ``map<str, str>``
    so we JSON-encode the provenance list. Downstream consumers
    parse with ``json.loads(contract['metadata']['labels']['provenance'])``.

    Returns ``None`` when the annotation log is empty so the
    label isn't emitted with a useless ``"[]"`` value.
    """
    import json as _json

    by_path = getattr(annotations, "by_path", None) or {}
    if not by_path:
        return None
    rows: List[Dict[str, Any]] = []
    for path in sorted(by_path):
        ann = by_path[path]
        confidence = getattr(ann, "confidence", None)
        provenance = getattr(ann, "provenance", None) or []
        rows.append(
            {
                "path": path,
                "confidence": (float(confidence.score) if confidence else None),
                "rationale": (getattr(confidence, "rationale", "") if confidence else ""),
                "sources": [
                    {
                        "kind": getattr(p, "kind", ""),
                        "ref": getattr(p, "ref", ""),
                        "snippet": getattr(p, "snippet", ""),
                    }
                    for p in provenance
                ],
            }
        )
    return _json.dumps(rows, separators=(",", ":"), sort_keys=False)


def build_contract_from_logical(
    logical: LogicalDraft,
    *,
    build_engine: str = "dbt",
    annotations: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a minimal valid contract for a logical draft.

    V1.5 (Gap 3) — when ``logical.source_summary`` carries aggregate
    catalog signal (populated by ``LogicalAgent.from_catalog``), this
    function promotes it into the emitted contract:

    * ``dominant_owner`` → ``metadata.owner.team``
    * ``dominant_domain`` → ``metadata.domain`` and top-level
      ``domain``
    * ``lineage_upstream`` → ``metadata.lineage.upstream[]``
    * ``classifications`` → ``metadata.classifications[]``
    * ``sensitivity_tags`` → labels (Fluid 0.7.x doesn't have a
      first-class ``agentPolicy.sensitiveData`` block; the labels
      hint surfaces the signal until the schema lands the field)
    * ``data_quality_score_min`` → ``labels.dataQualityScore``
    * ``freshness_sla_set`` (single value) → ``labels.freshnessSla``

    Contracts forged from intents / DDL (no catalog signal) get the
    legacy defaults — same behaviour as before V1.5.
    """
    slug = _slug(logical.name)
    summary = logical.source_summary or {}

    # Domain — catalog-aggregated dominant_domain wins; fall back to
    # the legacy ``source_summary["domain"]`` (for intents that name
    # the domain explicitly); final fallback is "analytics".
    domain = summary.get("dominant_domain") or summary.get("domain") or "analytics"

    # Owner — V1.5 priority: tag-based owner (intentional team
    # metadata from catalog tags like ``team`` / ``owner_team``)
    # wins; non-system table owner is the fallback; ultimately
    # ``data-team`` is the obvious-default placeholder when no
    # real owner can be determined. System roles like
    # ``ACCOUNTADMIN`` / ``SYSADMIN`` are NOT promoted to team
    # owner — they're privilege artefacts and surface as
    # ``labels.catalogCreatingRoles`` audit info instead.
    #
    # Surface metadata via the FLUID 0.7.2 schema's allowed shape
    # only — ``metadata`` is a closed object in the schema, so
    # extra fields land in ``labels`` (which is open-ended)
    # instead. When a future Fluid version adds typed ``ownership``
    # / ``classifications`` blocks we'll promote out of labels
    # into the typed shape.
    dominant_owner = summary.get("dominant_owner")
    owner_team = dominant_owner or "data-team"

    metadata: Dict[str, Any] = {
        "layer": "Logical",
        "owner": {"team": owner_team},
    }
    if summary.get("dominant_domain"):
        metadata["domain"] = summary["dominant_domain"]
    lineage_upstream = summary.get("lineage_upstream") or []
    if lineage_upstream:
        metadata["lineage"] = {"upstream": [{"fqn": fqn} for fqn in lineage_upstream]}
    labels: Dict[str, Any] = {
        "dataModelingTechnique": logical.technique,
        "modelSidecar": f"{slug}.fluid.yaml.model.json",
    }
    classifications = summary.get("classifications") or []
    if classifications:
        # Labels are string-valued; comma-join to keep the value
        # stable for downstream string-matching consumers.
        labels["catalogClassifications"] = ",".join(classifications)
    creating_roles = summary.get("creating_roles") or []
    if creating_roles:
        # Audit-only info — surfaces the system roles that created
        # the source tables (Snowflake ``ACCOUNTADMIN`` / etc.) so
        # a contract reviewer can tell "this owner was inferred
        # from a privilege artefact, not a real team tag." Pairs
        # with the ``catalogOwnerSource`` label below.
        labels["catalogCreatingRoles"] = ",".join(creating_roles)
    owner_source = summary.get("dominant_owner_source")
    if owner_source:
        # ``tag`` = catalog tag like ``team:analytics``; that's the
        # authoritative signal. ``table_owner`` = the creating
        # principal's identity (less authoritative but still real).
        # When neither is set, the contract's hardcoded
        # ``data-team`` default is in play and reviewers should
        # set a real team owner.
        labels["catalogOwnerSource"] = owner_source
    sensitivity_tags = summary.get("sensitivity_tags") or []
    if sensitivity_tags:
        # Fluid 0.7.x labels are string-valued; serialise the sorted
        # tag list as a comma-separated string. When the schema
        # adds a first-class ``agentPolicy.sensitiveData`` block,
        # we'll promote this to the typed shape.
        labels["sensitivityTags"] = ",".join(sensitivity_tags)
    if summary.get("source_catalog_name"):
        labels["sourceCatalog"] = summary["source_catalog_name"]
    quality_score = summary.get("data_quality_score_min")
    if quality_score is not None:
        labels["dataQualityScore"] = f"{float(quality_score):.2f}"
    freshness_set = summary.get("freshness_sla_set") or []
    if len(freshness_set) == 1:
        # When every table reports the same SLA, promote it cleanly.
        # Mixed SLAs land in metadata for the operator to triage.
        labels["freshnessSla"] = freshness_set[0]
    elif len(freshness_set) > 1:
        metadata["freshnessSlaSet"] = list(freshness_set)

    # Item 7 — emit per-claim provenance into the contract so
    # downstream tools (catalog publishers, governance dashboards,
    # compliance auditors) can trace WHERE every claim came from
    # and how strong the modeler's confidence was.
    #
    # Only emitted when the caller passed an
    # :class:`AnnotationLog`; preserves the v1.5 contract shape
    # for callers that don't have annotations yet.
    if annotations is not None:
        provenance_block = _build_provenance_block(annotations)
        if provenance_block:
            labels["provenance"] = provenance_block

    return {
        "fluidVersion": FluidSchemaManager.latest_bundled_version(),
        "kind": "DataProduct",
        "id": f"generated.{slug}",
        "name": logical.name.replace("_", " ").title(),
        "description": logical.description or f"Forged data model for {logical.name}",
        "domain": domain,
        "labels": labels,
        "metadata": metadata,
        "builds": [_default_build(build_engine, slug)],
        "exposes": [
            {
                "exposeId": slug,
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": f"runtime/{slug}.parquet"},
                },
                "contract": {
                    "schema": _expose_schema(logical),
                },
                "semantics": _fluid_semantics(logical),
            }
        ],
    }
