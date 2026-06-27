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

# NOTE: ``schema_manager`` (pulls heavy ``jsonschema``) is imported lazily at
# the two use sites below so it stays off the ``fluid mcp`` / ``fluid --help``
# / ``build_parser()`` cold path (this module is reached transitively from the
# mcp dispatcher at registration time).


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


def _expose_schema(
    logical: LogicalDraft,
    *,
    dataset_override: Optional[Any] = None,
    column_names: Optional[List[str]] = None,
    primary_keys: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Build the ``exposes[].contract.schema`` block.

    H8 fix (Snowflake e2e finding 06-snowflake-e2e.md):

    * ``dataset_override`` — when set, source columns from THIS
      specific OSI dataset rather than ``osi.datasets[0]``. Used
      by the DV2-per-artifact path so a hub expose carries
      hub columns, not the first-iterated dataset's columns.
    * ``column_names`` — explicit projection. When set, emit
      exactly these columns (in order) and look up each type
      from the global OSI field-type index. Used by DV2 hubs
      (project to business_key_columns), sats (project to
      attributes), and links (project to ``<hub>_hk``).
    * ``primary_keys`` — explicit list of names that should be
      marked ``required: true``. Used when the projection is
      a subset of the dataset.
    """
    # Build a type lookup across every OSI dataset — DV2 artifacts
    # often pull columns from join_keys that live on a different
    # dataset than the "owning" one.
    type_lookup: Dict[str, str] = {}
    description_lookup: Dict[str, str] = {}
    for d in logical.osi.datasets or []:
        for f in d.fields or []:
            if f.data_type:
                type_lookup.setdefault(f.name.lower(), f.data_type)
            if f.description:
                description_lookup.setdefault(f.name.lower(), f.description)

    if column_names is not None:
        pk_set = set(primary_keys or [])
        out: List[Dict[str, Any]] = []
        for column in column_names:
            entry: Dict[str, Any] = {
                "name": column,
                "type": type_lookup.get(column.lower(), "STRING"),
                "required": column in pk_set,
            }
            desc = description_lookup.get(column.lower())
            if desc:
                entry["description"] = desc
            out.append(entry)
        return out or [{"name": "id", "type": "STRING", "required": True}]

    dataset = (
        dataset_override
        if dataset_override is not None
        else (logical.osi.datasets[0] if logical.osi.datasets else None)
    )
    if dataset is not None:
        columns: List[Dict[str, Any]] = []
        for field in dataset.fields:
            # UX-9 fix: ``field.description`` carries the catalog
            # ``Comment`` / ``COMMENT`` / glossary blurb that the
            # adapter read off the source. The Fluid 0.7.x
            # ``column`` schema accepts ``description`` (string)
            # natively — we just have to forward it. Empty strings
            # are dropped so contracts forged from sources without
            # comments stay clean.
            entry: Dict[str, Any] = {
                "name": field.name,
                "type": field.data_type or "STRING",
                "required": field.name in dataset.primary_key,
            }
            if field.description:
                entry["description"] = field.description
            columns.append(entry)
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


def _fluid_semantics(
    logical: LogicalDraft, *, dataset_override: Optional[Any] = None
) -> Dict[str, Any]:
    """Build the semantics block for one expose.

    H8 fix (Snowflake e2e finding 06-snowflake-e2e.md): when the
    caller passes ``dataset_override`` (a specific
    :class:`OSIDataset`), build the semantics block off THAT dataset
    instead of always ``logical.osi.datasets[0]``. The dimensional
    and DV2 emitters now build one expose per artifact (fact, dim,
    hub, link, satellite); each expose's semantics must reflect the
    artifact's own columns, not the first-iterated dataset's.

    When ``dataset_override`` is ``None`` (intent / DDL forges with
    no DV2 / dimensional shape), fall back to the legacy behaviour:
    the first OSI dataset drives the semantics.
    """
    semantics: Dict[str, Any] = {
        "name": (dataset_override.name if dataset_override is not None else logical.osi.name),
        "description": logical.osi.description or logical.description,
    }
    datasets = logical.osi.datasets or []
    if dataset_override is not None:
        first_dataset = dataset_override
    else:
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


# --------------------------------------------------------------------
# H7 / H8 (Snowflake e2e finding 06-snowflake-e2e.md) — binding helpers
# --------------------------------------------------------------------
#
# H7: when a forge runs against a warehouse catalog
# (``source_kind == "catalog"`` and ``source_catalog_name`` is one of
# {snowflake, bigquery, snowflake, redshift, ...}), the emitted
# ``exposes[].binding`` MUST default to the warehouse, not
# ``local/parquet/runtime/<slug>.parquet``. Worst-case interop failure:
# downstream tooling sees a Snowflake-sourced data product as a local
# parquet file.
#
# H8: when the logical model is DV2-shaped, emit ONE expose per
# physical artifact (hub / link / sat). Previously 90 artifacts
# collapsed into a single 5-column expose carrying only the
# last-iterated table's columns.

_WAREHOUSE_CATALOG_BINDINGS: Dict[str, Dict[str, str]] = {
    # adapter name → default binding hints
    "snowflake": {"platform": "snowflake", "format": "snowflake_table"},
    "unity": {"platform": "databricks", "format": "delta_table"},
    "bigquery": {"platform": "gcp", "format": "bigquery_table"},
    "dataplex": {"platform": "gcp", "format": "bigquery_table"},
    "glue": {"platform": "aws", "format": "parquet"},
}


def _binding_for_artifact(
    *,
    logical: LogicalDraft,
    summary: Dict[str, Any],
    artifact_slug: str,
    source_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the ``binding`` block for one expose.

    Catalog-aware (H7):

    * Snowflake source → ``binding.platform: snowflake``,
      ``format: snowflake_table``, with ``database`` / ``schema``
      / ``table`` location keys pulled from the per-table
      bindings map (``source_summary["source_table_bindings"]``)
      or the scope-level defaults (``source_database`` /
      ``source_schema``).
    * BigQuery / Glue / Unity catalogs map similarly via
      ``_WAREHOUSE_CATALOG_BINDINGS``.
    * No catalog source (intent / DDL forge) → legacy
      ``local/parquet/runtime/<slug>.parquet`` default.

    ``source_tables`` is the artifact's ``mapped_source_tables``
    list (case-folded match against the per-table bindings).
    The first match wins; missing/empty falls through to the
    scope-level defaults.
    """
    catalog_name = (summary.get("source_catalog_name") or "").lower()
    binding_template = _WAREHOUSE_CATALOG_BINDINGS.get(catalog_name)
    if binding_template is None:
        # Non-catalog source (intent / DDL) — preserve the legacy
        # local/parquet default. Operators forging from a real
        # filesystem path should rebind manually post-forge.
        return {
            "platform": "local",
            "format": "parquet",
            "location": {"path": f"runtime/{artifact_slug}.parquet"},
        }

    binding: Dict[str, Any] = {
        "platform": binding_template["platform"],
        "format": binding_template["format"],
    }
    location: Dict[str, Any] = {}

    # Per-table binding lookup. The first mapped source table that
    # matches a known table name wins.
    table_bindings: Dict[str, Dict[str, Any]] = summary.get("source_table_bindings") or {}
    matched: Optional[Dict[str, Any]] = None
    for src in source_tables or []:
        key = (src or "").strip().lower()
        if key in table_bindings:
            matched = table_bindings[key]
            break

    if matched:
        for k in ("database", "schema", "table"):
            if matched.get(k):
                location[k] = matched[k]
    else:
        # Scope-level fallback (when the artifact's source table
        # wasn't recorded in the per-table map — e.g. DV2 links
        # whose mapped_source_tables[] is empty).
        if summary.get("source_database"):
            location["database"] = summary["source_database"]
        if summary.get("source_schema"):
            location["schema"] = summary["source_schema"]
        # No table name available — leave the location.table off
        # for the link / cross-cutting artifacts.

    if location:
        binding["location"] = location
    return binding


def _build_flat_exposes(
    *,
    logical: LogicalDraft,
    summary: Dict[str, Any],
    contract_slug: str,
) -> List[Dict[str, Any]]:
    """Source-aligned (``flat``) emit: one expose per OSI dataset — 1:1 with the
    source tables, no vault/dimensional reshaping (issue #248). Mirrors the
    single-expose builder but loops over datasets, so a multi-table source does
    not collapse into one expose. Degenerates to a single expose when the
    logical model carries no datasets.
    """
    datasets = logical.osi.datasets or []
    if not datasets:
        return [
            {
                "exposeId": contract_slug,
                "kind": "table",
                "version": "1.0.0",
                "binding": _binding_for_artifact(
                    logical=logical,
                    summary=summary,
                    artifact_slug=contract_slug,
                    source_tables=None,
                ),
                "contract": {"schema": _expose_schema(logical)},
                "semantics": _fluid_semantics(logical),
            }
        ]
    exposes: List[Dict[str, Any]] = []
    for dataset in datasets:
        slug = _slug(dataset.name)
        expose: Dict[str, Any] = {
            "exposeId": slug,
            "kind": "table",
            "version": "1.0.0",
            "binding": _binding_for_artifact(
                logical=logical,
                summary=summary,
                artifact_slug=slug,
                source_tables=[dataset.name],
            ),
            "contract": {"schema": _expose_schema(logical, dataset_override=dataset)},
            "semantics": _fluid_semantics(logical, dataset_override=dataset),
        }
        if dataset.description:
            expose["description"] = dataset.description
        exposes.append(expose)
    return exposes


def _build_dv2_exposes(
    *,
    logical: LogicalDraft,
    summary: Dict[str, Any],
    contract_slug: str,
) -> List[Dict[str, Any]]:
    """Emit one expose per DV2 artifact (H8).

    Order: hubs → links → satellites. Stable across runs.

    Each expose carries:
    * ``exposeId`` = the artifact's physical table name
    * ``kind`` = ``"table"``
    * ``binding`` = catalog-aware (H7) — Snowflake source bindings
      route to ``platform: snowflake`` with per-table location
    * ``contract.schema`` = projected to the artifact's columns,
      typed via the global OSI field-type index (so STRINGs
      become NUMBER / TIMESTAMP_TZ / TEXT etc. per the source)
    * ``semantics`` = minimal per-artifact block — primary
      entity = the artifact, dimensions = its columns
    * ``labels.dataVaultArtifactType`` = ``"hub"`` / ``"link"`` /
      ``"satellite"`` so downstream consumers (catalog publishers,
      dbt-vault transformation generators) can group artifacts.
    """
    if logical.dv2 is None:
        return []
    exposes: List[Dict[str, Any]] = []
    technique_label = "data_vault_2"

    def _artifact_semantics(
        *,
        artifact_name: str,
        primary_keys: List[str],
        columns: List[str],
    ) -> Dict[str, Any]:
        """Minimal per-artifact semantics block.

        Every DV2 expose still needs a valid semantics block (the
        validator pins ``entities`` / ``dimensions`` / ``measures``
        / ``metrics`` as required non-empty lists). Build the
        minimum so validation passes — downstream agents can
        enrich later.
        """
        primary_entity_key = primary_keys[0] if primary_keys else artifact_name
        entities = [
            {
                "name": artifact_name,
                "type": "primary",
                "expr": primary_entity_key,
            }
        ]
        dims: List[Dict[str, Any]] = []
        for column in columns:
            doc: Dict[str, Any] = {"name": column, "type": "categorical", "expr": column}
            if _is_time_name(column):
                doc["type"] = "time"
                doc["typeParams"] = {"timeGranularity": "day"}
            dims.append(doc)
        if not dims:
            # Schema validator requires at least one dimension —
            # synthesize one from the artifact name.
            dims.append({"name": artifact_name, "type": "categorical", "expr": artifact_name})
        return {
            "name": artifact_name,
            "description": (
                logical.osi.description
                or logical.description
                or f"Data Vault 2 artifact {artifact_name}."
            ),
            "entities": entities,
            "dimensions": dims,
            "measures": [{"name": "record_count", "agg": "count", "expr": "*"}],
            "metrics": [
                {
                    "name": "record_count",
                    "type": "simple",
                    "measure": "record_count",
                    "description": "Count of records.",
                }
            ],
        }

    for hub in logical.dv2.hubs:
        columns = list(hub.business_key_columns) or [f"{_slug(hub.entity_name)}_id"]
        exposes.append(
            {
                "exposeId": hub.hub_table_name,
                "kind": "table",
                "version": "1.0.0",
                "labels": {
                    "dataVaultArtifactType": "hub",
                    "dataModelingTechnique": technique_label,
                    "dvEntityName": hub.entity_name,
                },
                "binding": _binding_for_artifact(
                    logical=logical,
                    summary=summary,
                    artifact_slug=hub.hub_table_name,
                    source_tables=list(hub.mapped_source_tables or []),
                ),
                "contract": {
                    "schema": _expose_schema(
                        logical,
                        column_names=columns,
                        primary_keys=columns,
                    ),
                },
                "semantics": _artifact_semantics(
                    artifact_name=hub.entity_name,
                    primary_keys=columns,
                    columns=columns,
                ),
            }
        )
    for link in logical.dv2.links:
        # Link columns are the per-hub hash-keys; the source-system
        # data types don't carry through (the hash is computed in
        # the DV2 transform layer). Emit them as STRING.
        columns = [f"{hub}_hk" for hub in link.hubs_involved] or [f"{link.link_table_name}_id"]
        exposes.append(
            {
                "exposeId": link.link_table_name,
                "kind": "table",
                "version": "1.0.0",
                "labels": {
                    "dataVaultArtifactType": "link",
                    "dataModelingTechnique": technique_label,
                    "dvLinkName": link.link_name,
                    "dvHubsInvolved": ",".join(link.hubs_involved),
                },
                "binding": _binding_for_artifact(
                    logical=logical,
                    summary=summary,
                    artifact_slug=link.link_table_name,
                    source_tables=None,
                ),
                "contract": {
                    "schema": _expose_schema(
                        logical,
                        column_names=columns,
                        primary_keys=columns,
                    ),
                },
                "semantics": _artifact_semantics(
                    artifact_name=link.link_name,
                    primary_keys=columns,
                    columns=columns,
                ),
            }
        )
    for sat in logical.dv2.satellites:
        columns = list(sat.attributes) or ["hash_diff"]
        exposes.append(
            {
                "exposeId": sat.satellite_table_name,
                "kind": "table",
                "version": "1.0.0",
                "labels": {
                    "dataVaultArtifactType": "satellite",
                    "dataModelingTechnique": technique_label,
                    "dvParentHub": sat.parent_hub,
                    "dvChangeTracking": sat.change_tracking,
                },
                "binding": _binding_for_artifact(
                    logical=logical,
                    summary=summary,
                    artifact_slug=sat.satellite_table_name,
                    source_tables=list(sat.mapped_source_tables or []),
                ),
                "contract": {
                    "schema": _expose_schema(
                        logical,
                        column_names=columns,
                        primary_keys=[],
                    ),
                },
                "semantics": _artifact_semantics(
                    artifact_name=sat.entity_name,
                    primary_keys=[],
                    columns=columns,
                ),
            }
        )
    return exposes


def _assemble_extensions(logical: LogicalDraft) -> Dict[str, Any]:
    """Promote schema-valid LLM-proposed ``contract.extensions.<key>`` blocks.

    Proposals live in the free-form
    ``logical.source_summary['proposed_extensions']`` map (populated when the
    modeler is grounded on installed extension schemas via the extension-schema
    prompt fragment). Only blocks whose key has an installed schema AND that
    validate against it are kept. Returns ``{}`` when there are no proposals or
    no matching installed schema — so contracts generated without extension
    plugins are byte-identical to before.
    """
    from fluid_build.extension_schemas import assemble_proposed_extensions
    from fluid_build.schema_manager import FluidSchemaManager

    proposed = (logical.source_summary or {}).get("proposed_extensions")
    return assemble_proposed_extensions(
        proposed, fluid_version=FluidSchemaManager.latest_bundled_version()
    )


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
    from fluid_build.schema_manager import FluidSchemaManager

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

    # H8 fix (Snowflake e2e finding 06-snowflake-e2e.md): for
    # DV2-shaped logical drafts, emit one expose per artifact
    # (hub / link / satellite). Previously a 90-artifact DV2 model
    # collapsed into a single 5-column expose carrying only the
    # last-iterated table's columns — a silent data-loss bug.
    exposes: List[Dict[str, Any]]
    if logical.dv2 is not None and (
        logical.dv2.hubs or logical.dv2.links or logical.dv2.satellites
    ):
        exposes = _build_dv2_exposes(
            logical=logical,
            summary=summary,
            contract_slug=slug,
        )
    elif logical.dv2 is None and logical.dimensional is None:
        # Source-aligned (``flat``) or a bring-your-own (``custom``) model with
        # neither vault nor dimensional branch: emit one expose per OSI dataset
        # (1:1 with the source tables) rather than collapsing to a single expose.
        exposes = _build_flat_exposes(
            logical=logical,
            summary=summary,
            contract_slug=slug,
        )
    else:
        # Single-expose path: intent / DDL / dimensional (the
        # dimensional emitter still keeps the legacy single-expose
        # shape — its semantics block already enumerates every
        # fact + dimension via ``logical.dimensional`` so the
        # collapse is intentional, not a data-loss bug).
        expose: Dict[str, Any] = {
            "exposeId": slug,
            "kind": "table",
            "version": "1.0.0",
            "binding": _binding_for_artifact(
                logical=logical,
                summary=summary,
                artifact_slug=slug,
                source_tables=None,
            ),
            "contract": {
                "schema": _expose_schema(logical),
            },
            "semantics": _fluid_semantics(logical),
        }
        # UX-9 fix: when the source catalog provided a table-level
        # ``Description`` / ``COMMENT`` (Glue, Snowflake, BQ, …) it
        # lands on ``logical.osi.datasets[0].description`` via
        # ``_translate_catalog_table`` → ``_osi_from_tables``. Forward
        # it to ``exposes[].description`` so the dataset blurb survives
        # end-to-end. Top-level contract ``description`` already maps
        # from ``logical.description`` (which carries the data-product
        # summary, not the source table comment) so we surface the
        # source table comment at the expose level where it semantically
        # belongs.
        if logical.osi.datasets:
            first_dataset = logical.osi.datasets[0]
            if first_dataset.description:
                expose["description"] = first_dataset.description
        exposes = [expose]

    contract: Dict[str, Any] = {
        "fluidVersion": FluidSchemaManager.latest_bundled_version(),
        "kind": "DataProduct",
        "id": f"generated.{slug}",
        "name": logical.name.replace("_", " ").title(),
        "description": logical.description or f"Forged data model for {logical.name}",
        "domain": domain,
        "labels": labels,
        "metadata": metadata,
        "builds": [_default_build(build_engine, slug)],
        "exposes": exposes,
    }
    # Promote any LLM-proposed, schema-valid contract.extensions.<key> blocks
    # (grounded via the extension-schema prompt fragment). Added only when
    # non-empty, so contracts with no extensions are byte-identical to before.
    extensions = _assemble_extensions(logical)
    if extensions:
        contract["extensions"] = extensions
    return contract
