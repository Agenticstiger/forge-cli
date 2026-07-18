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

"""Deterministic repairs for forged logical data-model drafts.

Provider-native structured output gets us close to a typed model, but
hosted and local LLMs still vary on list ordering, duplicate fields, and
small omissions. This module keeps those repairs in one deterministic
place so contracts, model docs, and dbt SQL are all emitted from the
same canonical sidecar.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Sequence, TypeVar

from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DV2Model,
    FieldDefinition,
    recommend_dimensional_variant,
)
from fluid_build.copilot.schemas.osi import OSIAIContext, OSIDataset, OSIField
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft

# Grain vocabulary is single-sourced (module-attribute access so test
# patches flow through) — this module previously carried its own drifted
# alias table.
from fluid_build.forge_datamodel import time_grains as _time_grains

_T = TypeVar("_T")


def canonicalize_logical_draft(logical: LogicalDraft) -> LogicalDraft:
    """Return a semantically equivalent, stable logical draft.

    The canonical form is deliberately conservative: it de-dupes exact
    duplicates, fills required DV2 keys when a provider omits them, and
    sorts repeatable collections by business identifiers. It does not
    invent business concepts beyond the minimum needed to keep the model
    valid and reproducible.
    """

    repaired = logical.model_copy(deep=True)
    if repaired.dv2 is not None:
        _canonicalize_dv2(repaired.dv2)
    if repaired.dimensional is not None:
        _canonicalize_dimensional(repaired.dimensional)
    _canonicalize_osi(repaired)
    repaired.review_notes = _unique_strings(repaired.review_notes)
    return LogicalDraft.model_validate(repaired.model_dump(mode="json", by_alias=True))


def _canonicalize_dv2(model: DV2Model) -> None:
    hubs_by_table: dict[str, Any] = {}
    for hub in model.hubs:
        hub.hub_table_name = hub.hub_table_name or f"hub_{_slug(hub.entity_name)}"
        hub.business_key_columns = _unique_strings(
            hub.business_key_columns or [f"{_slug(hub.entity_name)}_id"]
        )
        hub.mapped_source_tables = _unique_strings(hub.mapped_source_tables)
        hubs_by_table.setdefault(hub.hub_table_name, hub)
    model.hubs = sorted(hubs_by_table.values(), key=lambda hub: _sort_text(hub.hub_table_name))

    known_hubs = {hub.hub_table_name for hub in model.hubs}
    entity_to_hub = {_slug(hub.entity_name): hub.hub_table_name for hub in model.hubs}
    first_hub = model.hubs[0].hub_table_name if model.hubs else None

    links_by_table: dict[str, Any] = {}
    for link in model.links:
        link.link_table_name = link.link_table_name or f"lnk_{_slug(link.link_name)}"
        link.hubs_involved = _unique_strings(
            hub for hub in link.hubs_involved if not known_hubs or hub in known_hubs
        )
        link.join_keys = _unique_by(
            link.join_keys,
            lambda key: (
                _sort_text(key.table1),
                _sort_text(key.column1),
                _sort_text(key.table2),
                _sort_text(key.column2),
            ),
        )
        link.relationships = _unique_by(
            link.relationships,
            lambda rel: (
                _sort_text(rel.source_entity),
                _sort_text(rel.target_entity),
                _sort_text(rel.join_condition),
            ),
        )
        links_by_table.setdefault(link.link_table_name, link)
    model.links = sorted(links_by_table.values(), key=lambda link: _sort_text(link.link_table_name))

    satellites_by_table: dict[str, Any] = {}
    for sat in model.satellites:
        sat.satellite_table_name = (
            sat.satellite_table_name or f"sat_{_slug(sat.entity_name)}_details"
        )
        if not sat.parent_hub or sat.parent_hub not in known_hubs:
            sat.parent_hub = (
                entity_to_hub.get(_slug(sat.entity_name))
                or _infer_parent_hub_from_satellite_name(sat.satellite_table_name, entity_to_hub)
                or first_hub
                or f"hub_{_slug(sat.entity_name)}"
            )
        sat.attributes = _unique_strings(sat.attributes)
        sat.mapped_source_tables = _unique_strings(sat.mapped_source_tables)
        satellites_by_table.setdefault(sat.satellite_table_name, sat)
    model.satellites = sorted(
        satellites_by_table.values(),
        key=lambda sat: (_sort_text(sat.parent_hub), _sort_text(sat.satellite_table_name)),
    )

    for pit in model.pits:
        if not pit.parent_hub or pit.parent_hub not in known_hubs:
            pit.parent_hub = first_hub or pit.parent_hub
        pit.satellites = _unique_strings(pit.satellites)
    model.pits = sorted(model.pits, key=lambda pit: _sort_text(pit.pit_table_name))

    for bridge in model.bridges:
        bridge.source_links = _unique_strings(bridge.source_links)
    model.bridges = sorted(model.bridges, key=lambda bridge: _sort_text(bridge.bridge_table_name))


def _canonicalize_dimensional(model: DimensionalModel) -> None:
    facts_by_name: dict[str, Any] = {}
    for fact in model.facts:
        fact.measures = _unique_fields(fact.measures)
        fact.foreign_keys = _unique_strings(fact.foreign_keys)
        fact.degenerate_dimensions = _unique_strings(fact.degenerate_dimensions)
        facts_by_name.setdefault(fact.name, fact)
    model.facts = sorted(facts_by_name.values(), key=lambda fact: _sort_text(fact.name))

    dimensions_by_name: dict[str, Any] = {}
    for dim in model.dimensions:
        dim.attributes = _unique_fields(dim.attributes, primary_keys=dim.natural_keys)
        dim.natural_keys = _unique_strings(dim.natural_keys)
        dimensions_by_name.setdefault(dim.name, dim)
    model.dimensions = sorted(dimensions_by_name.values(), key=lambda dim: _sort_text(dim.name))

    model.conformed_dimensions = _unique_strings(model.conformed_dimensions)
    model.bridges = _unique_strings(model.bridges)
    model.degenerate_dims = _unique_strings(model.degenerate_dims)
    model.slowly_changing = {
        key: model.slowly_changing[key] for key in sorted(model.slowly_changing, key=_sort_text)
    }
    model.variant = recommend_dimensional_variant(model)


def _canonicalize_osi(logical: LogicalDraft) -> None:
    osi = logical.osi
    osi.ai_context = _canonical_ai_context(osi.ai_context)

    datasets_by_name: dict[str, OSIDataset] = {}
    for dataset in osi.datasets:
        dataset.primary_key = _unique_strings(dataset.primary_key)
        dataset.unique_keys = _unique_key_sets(dataset.unique_keys)
        dataset.fields = _canonical_fields(dataset.fields, primary_keys=dataset.primary_key)
        if dataset.ai_context is not None:
            dataset.ai_context = _canonical_ai_context(dataset.ai_context)
        datasets_by_name.setdefault(dataset.name, dataset)
    osi.datasets = sorted(
        datasets_by_name.values(),
        key=lambda dataset: (_dataset_rank(dataset.name), _sort_text(dataset.name)),
    )

    relationships_by_key: dict[tuple[str, str, str], Any] = {}
    for relationship in osi.relationships:
        relationship.from_columns = _unique_strings(relationship.from_columns)
        relationship.to_columns = _unique_strings(relationship.to_columns)
        if relationship.ai_context is not None:
            relationship.ai_context = _canonical_ai_context(relationship.ai_context)
        relationships_by_key.setdefault(
            (
                _sort_text(relationship.name),
                _sort_text(relationship.from_),
                _sort_text(relationship.to),
            ),
            relationship,
        )
    osi.relationships = [relationships_by_key[key] for key in sorted(relationships_by_key)]

    metrics_by_name: dict[str, Any] = {}
    for metric in osi.metrics:
        if metric.ai_context is not None:
            metric.ai_context = _canonical_ai_context(metric.ai_context)
        metrics_by_name.setdefault(metric.name, metric)
    osi.metrics = [metrics_by_name[name] for name in sorted(metrics_by_name, key=_sort_text)]


def _canonical_fields(fields: Sequence[OSIField], *, primary_keys: Sequence[str]) -> List[OSIField]:
    by_name: dict[str, OSIField] = {}
    for field in fields:
        if field.dimension is not None and field.dimension.grain:
            field.dimension.grain = _time_grains.resolve_grain_alias(field.dimension.grain)
        if field.ai_context is not None:
            field.ai_context = _canonical_ai_context(field.ai_context)
        by_name.setdefault(field.name, field)
    pk = {str(name).lower() for name in primary_keys}
    return sorted(
        by_name.values(),
        key=lambda field: (0 if field.name.lower() in pk else 1, _sort_text(field.name)),
    )


def _unique_fields(
    fields: Sequence[FieldDefinition], *, primary_keys: Sequence[str] = ()
) -> List[FieldDefinition]:
    by_name: dict[str, FieldDefinition] = {}
    for field in fields:
        field.source_columns = _unique_strings(field.source_columns)
        by_name.setdefault(field.name, field)
    pk = {str(name).lower() for name in primary_keys}
    return sorted(
        by_name.values(),
        key=lambda field: (0 if field.name.lower() in pk else 1, _sort_text(field.name)),
    )


def _canonical_ai_context(context: OSIAIContext) -> OSIAIContext:
    context.synonyms = _unique_strings(context.synonyms)
    context.examples = _unique_strings(context.examples)
    return context


def _unique_strings(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return sorted(out, key=_sort_text)


def _unique_by(values: Sequence[_T], key_fn: Any) -> List[_T]:
    by_key: dict[Any, _T] = {}
    for value in values:
        by_key.setdefault(key_fn(value), value)
    return [by_key[key] for key in sorted(by_key)]


def _unique_key_sets(values: Sequence[Sequence[str]]) -> List[List[str]]:
    seen: set[tuple[str, ...]] = set()
    out: List[List[str]] = []
    for key_set in values or []:
        normalized = tuple(_unique_strings(key_set))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(list(normalized))
    return sorted(out, key=lambda item: tuple(_sort_text(value) for value in item))


def _dataset_rank(name: str) -> int:
    normalized = _sort_text(name)
    if normalized.startswith(("fact_", "fct_")):
        return 0
    if normalized.startswith("hub_"):
        return 1
    if normalized.startswith(("dim_", "dimension_")):
        return 2
    return 3


def _infer_parent_hub_from_satellite_name(
    satellite_name: str,
    entity_to_hub: dict[str, str],
) -> Optional[str]:
    slug = _slug(satellite_name)
    for entity, hub in sorted(entity_to_hub.items()):
        if entity and entity in slug:
            return hub
    return None


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", str(text).strip().lower()).strip("_")
    return value or "model"


def _sort_text(value: str) -> str:
    return str(value or "").strip().lower()
