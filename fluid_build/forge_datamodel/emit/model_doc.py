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

"""Human-readable Markdown + Mermaid emission for forged logical models."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from fluid_build.copilot.schemas.stage_outputs import LogicalDraft


def emit_model_markdown(logical: LogicalDraft) -> str:
    """Render a reviewable model document from the canonical logical sidecar."""
    lines: list[str] = [
        f"# {_display_name(logical.name)} Data Model",
        "",
        f"- Technique: {_technique_label(logical.technique)}",
    ]
    if logical.description:
        lines.append(f"- Description: {logical.description}")
    source_hint = _source_hint(logical.source_summary)
    if source_hint:
        lines.append(f"- Source hint: {source_hint}")

    lines.extend(["", "## Mermaid Diagram", "", "```mermaid"])
    if logical.technique == "data_vault_2" and logical.dv2 is not None:
        lines.extend(_dv2_mermaid(logical))
    elif logical.technique == "dimensional" and logical.dimensional is not None:
        lines.extend(_dimensional_mermaid(logical))
    else:
        lines.append("flowchart LR")
        lines.append('  model["No model objects emitted"]')
    lines.extend(["```", ""])

    if logical.technique == "data_vault_2" and logical.dv2 is not None:
        lines.extend(_dv2_inventory(logical))
    elif logical.technique == "dimensional" and logical.dimensional is not None:
        lines.extend(_dimensional_inventory(logical))

    lines.extend(_semantic_inventory(logical))
    lines.extend(_source_and_assumptions(logical))
    return "\n".join(lines).rstrip() + "\n"


def _display_name(value: str) -> str:
    text = str(value or "model").replace("_", " ").strip()
    return " ".join(word.capitalize() for word in text.split()) or "Model"


def _technique_label(value: str) -> str:
    return {
        "data_vault_2": "Data Vault 2.0",
        "dimensional": "Dimensional / Kimball",
    }.get(value, value)


def _source_hint(source_summary: Mapping[str, Any]) -> str:
    if not source_summary:
        return ""
    candidates = [
        source_summary.get("source_kind"),
        source_summary.get("source_type"),
        source_summary.get("database"),
        source_summary.get("schema"),
    ]
    return ", ".join(str(item) for item in candidates if item)


def _safe_id(value: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "node")).strip("_") or "node"
    if base[0].isdigit():
        base = f"n_{base}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _label(*parts: str) -> str:
    clean = [str(part).replace('"', "'").strip() for part in parts if str(part or "").strip()]
    return "\\n".join(clean)


def _csv(values: Iterable[Any], *, fallback: str = "-") -> str:
    items = [str(value).strip() for value in values if str(value or "").strip()]
    return ", ".join(items) if items else fallback


def _dv2_mermaid(logical: LogicalDraft) -> list[str]:
    assert logical.dv2 is not None
    lines = ["flowchart LR"]
    used: set[str] = set()
    hub_nodes: dict[str, str] = {}
    for hub in logical.dv2.hubs:
        node = _safe_id(hub.hub_table_name, used)
        hub_nodes[hub.hub_table_name.lower()] = node
        hub_nodes[hub.entity_name.lower()] = node
        lines.append(
            f'  {node}["{_label(hub.hub_table_name, "Hub: " + hub.entity_name, "BK: " + _csv(hub.business_key_columns))}"]'
        )

    link_nodes: dict[str, str] = {}
    for link in logical.dv2.links:
        node = _safe_id(link.link_table_name, used)
        link_nodes[link.link_table_name.lower()] = node
        lines.append(
            f'  {node}["{_label(link.link_table_name, "Link", "Hubs: " + _csv(link.hubs_involved))}"]'
        )

    for sat in logical.dv2.satellites:
        node = _safe_id(sat.satellite_table_name, used)
        lines.append(
            f'  {node}["{_label(sat.satellite_table_name, "Satellite: " + sat.entity_name, "Attrs: " + _csv(sat.attributes[:4]))}"]'
        )
        parent = hub_nodes.get(sat.parent_hub.lower()) or hub_nodes.get(sat.entity_name.lower())
        if parent:
            lines.append(f"  {parent} --> {node}")

    for link in logical.dv2.links:
        link_node = link_nodes.get(link.link_table_name.lower())
        if not link_node:
            continue
        for hub_name in link.hubs_involved:
            hub_node = hub_nodes.get(str(hub_name).lower())
            if hub_node:
                lines.append(f"  {hub_node} --- {link_node}")

    return lines if len(lines) > 1 else ["flowchart LR", '  model["No DV2 objects emitted"]']


def _dimensional_mermaid(logical: LogicalDraft) -> list[str]:
    assert logical.dimensional is not None
    lines = ["flowchart LR"]
    used: set[str] = set()
    dim_nodes: dict[str, str] = {}
    for dim in logical.dimensional.dimensions:
        node = _safe_id(dim.name, used)
        dim_nodes[dim.name.lower()] = node
        if dim.surrogate_key:
            dim_nodes[dim.surrogate_key.lower()] = node
        for key in dim.natural_keys:
            dim_nodes[str(key).lower()] = node
        lines.append(
            f'  {node}["{_label(dim.name, "Dimension", "NK: " + _csv(dim.natural_keys))}"]'
        )

    fact_nodes: dict[str, str] = {}
    for fact in logical.dimensional.facts:
        node = _safe_id(fact.name, used)
        fact_nodes[fact.name.lower()] = node
        measures = [measure.name for measure in fact.measures]
        lines.append(
            f'  {node}["{_label(fact.name, "Fact", "Grain: " + fact.grain_statement, "Measures: " + _csv(measures[:4]))}"]'
        )
        matched_dims = _matching_dimension_nodes(fact.foreign_keys, dim_nodes)
        if not matched_dims and not fact.foreign_keys and len(logical.dimensional.dimensions) <= 8:
            matched_dims = list(dict.fromkeys(dim_nodes.values()))
        for dim_node in matched_dims:
            lines.append(f"  {node} --> {dim_node}")

    return (
        lines if len(lines) > 1 else ["flowchart LR", '  model["No dimensional objects emitted"]']
    )


def _matching_dimension_nodes(
    foreign_keys: Iterable[str], dim_nodes: Mapping[str, str]
) -> list[str]:
    matches: list[str] = []
    for key in foreign_keys:
        normalized = str(key).lower()
        for dim_key, node in dim_nodes.items():
            if dim_key and (dim_key in normalized or normalized in dim_key):
                matches.append(node)
    return list(dict.fromkeys(matches))


def _dv2_inventory(logical: LogicalDraft) -> list[str]:
    assert logical.dv2 is not None
    lines = ["## Model Inventory", "", "### Hubs"]
    if logical.dv2.hubs:
        for hub in logical.dv2.hubs:
            sources = _csv(hub.mapped_source_tables)
            lines.append(
                f"- `{hub.hub_table_name}`: entity `{hub.entity_name}`, business keys `{_csv(hub.business_key_columns)}`, sources `{sources}`."
            )
    else:
        lines.append("- None.")

    lines.extend(["", "### Links"])
    if logical.dv2.links:
        for link in logical.dv2.links:
            join_keys = [
                f"{key.table1}.{key.column1} = {key.table2}.{key.column2}" for key in link.join_keys
            ]
            lines.append(
                f"- `{link.link_table_name}`: hubs `{_csv(link.hubs_involved)}`, join keys `{_csv(join_keys)}`."
            )
    else:
        lines.append("- None.")

    lines.extend(["", "### Satellites"])
    if logical.dv2.satellites:
        for sat in logical.dv2.satellites:
            lines.append(
                f"- `{sat.satellite_table_name}`: parent `{sat.parent_hub}`, attributes `{_csv(sat.attributes)}`, tracking `{sat.change_tracking}`."
            )
    else:
        lines.append("- None.")
    lines.append("")
    return lines


def _dimensional_inventory(logical: LogicalDraft) -> list[str]:
    assert logical.dimensional is not None
    lines = ["## Model Inventory", "", "### Facts"]
    if logical.dimensional.facts:
        for fact in logical.dimensional.facts:
            measures = [f"{measure.name} ({measure.data_type})" for measure in fact.measures]
            lines.append(
                f"- `{fact.name}`: grain `{fact.grain_statement}`, foreign keys `{_csv(fact.foreign_keys)}`, measures `{_csv(measures)}`."
            )
    else:
        lines.append("- None.")

    lines.extend(["", "### Dimensions"])
    if logical.dimensional.dimensions:
        for dim in logical.dimensional.dimensions:
            attrs = [field.name for field in dim.attributes]
            lines.append(
                f"- `{dim.name}`: natural keys `{_csv(dim.natural_keys)}`, surrogate key `{dim.surrogate_key or '-'}`, attributes `{_csv(attrs)}`."
            )
    else:
        lines.append("- None.")
    lines.append("")
    return lines


def _semantic_inventory(logical: LogicalDraft) -> list[str]:
    lines = ["## Semantic Layer", ""]
    datasets = logical.osi.datasets if logical.osi else []
    if datasets:
        lines.append("### Datasets")
        for dataset in datasets:
            grain = _csv(dataset.primary_key, fallback="not specified")
            fields = [field.name for field in dataset.fields]
            lines.append(
                f"- `{dataset.name}`: source `{dataset.source or '-'}`, grain/primary key `{grain}`, fields `{_csv(fields[:12])}`."
            )
        lines.append("")

    dimensions: list[str] = []
    measures: list[str] = []
    for dataset in datasets:
        for field in dataset.fields:
            if field.dimension is not None:
                suffix = f" ({field.dimension.grain})" if field.dimension.grain else ""
                dimensions.append(f"{field.name}{suffix}")
            elif field.data_type and str(field.data_type).lower() in {
                "number",
                "numeric",
                "integer",
                "float",
                "decimal",
            }:
                measures.append(field.name)
    if dimensions:
        lines.append(f"- Dimensions: {_csv(dimensions)}")
    if measures:
        lines.append(f"- Measures: {_csv(measures)}")
    if logical.osi.metrics:
        lines.append(f"- Metrics: {_csv(metric.name for metric in logical.osi.metrics)}")
    if not datasets and not logical.osi.metrics:
        lines.append("- No semantic datasets or metrics emitted.")
    lines.append("")
    return lines


def _source_and_assumptions(logical: LogicalDraft) -> list[str]:
    lines = ["## Sources And Assumptions", ""]
    if logical.source_summary:
        for key in sorted(logical.source_summary):
            value = logical.source_summary[key]
            if value not in (None, "", [], {}):
                lines.append(f"- {key}: {value}")
    if logical.review_notes:
        for note in logical.review_notes:
            lines.append(f"- Assumption: {note}")
    if len(lines) == 2:
        lines.append("- No additional source hints or assumptions recorded.")
    lines.append("")
    return lines


__all__ = ["emit_model_markdown"]
