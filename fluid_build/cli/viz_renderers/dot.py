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

"""DOT renderer — emits Graphviz DOT source from a parsed FLUID contract. Extracted from ``viz_graph.py`` so the renderer's ~200 LOC of node/edge generation lives in its own module.

The public entry point :func:`_build_contract_dot` is re-imported by
``viz_graph`` at top level so existing test patches that target
``fluid_build.cli.viz_graph._build_contract_dot`` still resolve.
"""

from __future__ import annotations

import html as html_module
import logging
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from fluid_build.cli.viz_graph import (
    THEMES,
    GraphConfig,
    GraphMetrics,
    _escape_label,
    _get_theme_value,
    _safe_id,
)


def _build_contract_dot(
    contract: Mapping[str, Any],
    *,
    theme: str,
    rankdir: str,
    title: Optional[str],
    legend: bool,
    collapse_consumes: bool,
    collapse_exposes: bool,
    plan: Optional[Mapping[str, Any]],
) -> str:
    t = THEMES.get(theme, THEMES["dark"])

    c_id = contract.get("id", "product")
    c_name = contract.get("name") or c_id
    meta = contract.get("metadata") or {}
    domain = contract.get("domain", "Unknown")
    layer = meta.get("layer", "N/A")

    consumes: Sequence[Mapping[str, Any]] = contract.get("consumes") or []
    exposes: Sequence[Mapping[str, Any]] = contract.get("exposes") or []

    # Nodes
    product_node = _safe_id(f"product_{c_id}")
    consume_nodes: List[Tuple[str, str]] = []  # (node_id, label)
    expose_nodes: List[Tuple[str, str]] = []

    if collapse_consumes and consumes:
        consume_nodes.append(("consumes_agg", "Consumes…"))
    else:
        for c in consumes:
            rid = str(c.get("ref") or c.get("id") or "source")
            nid = _safe_id(f"consume_{rid}")
            lbl_top = c.get("id") or "source"
            lbl_bot = rid
            consume_nodes.append((nid, f"{lbl_top}\\n{lbl_bot}"))

    if collapse_exposes and exposes:
        expose_nodes.append(("exposes_agg", "Exposes…"))
    else:
        for e in exposes:
            eid = str(e.get("id") or "expose")
            et = str(e.get("type") or "")
            loc = e.get("location") or {}
            fmt = loc.get("format") or ""
            nid = _safe_id(f"expose_{eid}")
            lbl_top = eid
            lbl_bot = f"{et or 'artifact'} {f'[{fmt}]' if fmt else ''}".strip()
            expose_nodes.append((nid, f"{lbl_top}\\n{lbl_bot}"))

    # Plan nodes (optional)
    plan_nodes: List[Tuple[str, str]] = []
    plan_edges: List[Tuple[str, str]] = []
    if plan and isinstance(plan.get("actions"), list) and plan["actions"]:
        # Chain actions A->B->C, then link product to first action and last action to exposes
        prev = None
        for i, a in enumerate(plan["actions"]):
            op = str(a.get("op", "action"))
            nid = _safe_id(f"action_{i}_{op}")
            label = op
            if "dataset" in a and "table" in a:
                label = f"{op}\\n{a['dataset']}.{a['table']}"
            elif "name" in a:
                label = f"{op}\\n{a['name']}"
            elif "dst" in a:
                label = f"{op}\\n{a['dst']}"
            plan_nodes.append((nid, label))
            if prev:
                plan_edges.append((prev, nid))
            prev = nid

    # Build DOT
    lines: List[str] = []
    lines.append("digraph G {")
    lines.append(
        f'  graph [bgcolor="{t["bg"]}", color="{t["grid"]}", fontname="{t["font"]}", labeljust="l"];'
    )
    lines.append(f'  node [fontname="{t["font"]}", color="{t["fg"]}", fontcolor="{t["fg"]}"];')
    lines.append(f'  edge [color="{t["edge"]}", arrowsize=0.8];')
    lines.append(f"  rankdir={rankdir};")

    # Title
    graph_title = title or f"{c_name}  •  Domain: {domain}  •  Layer: {layer}"
    lines.append('  labelloc="t";')
    lines.append(f'  label="{_escape_label(graph_title)}";')

    # Clusters: Domain/Layer around Product; Consumes; Exposes; Plan
    # Product cluster
    lines.append("  subgraph cluster_product {")
    lines.append('    label="Data Product";')
    lines.append(f'    color="{t["cluster_border"]}";')
    lines.append('    style="rounded,filled";')
    lines.append(f'    fillcolor="{t["cluster_fill"]}";')
    lines.append(
        f'    {product_node} [shape=box, style="rounded,filled", fillcolor="{t["product_fill"]}", '
        f'color="{t["product_border"]}", penwidth=2, label="{_escape_label(c_name)}\\n({_escape_label(c_id)})"];'
    )
    # Domain & Layer “tags”
    tag_domain = _safe_id(f"tag_domain_{domain}")
    tag_layer = _safe_id(f"tag_layer_{layer}")
    lines.append(
        f'    {tag_domain} [shape=note, style="filled", fontsize=10, fillcolor="{t["expose_fill"]}", '
        f'color="{t["expose_border"]}", label="Domain: {_escape_label(domain)}"];'
    )
    lines.append(
        f'    {tag_layer} [shape=note, style="filled", fontsize=10, fillcolor="{t["expose_fill"]}", '
        f'color="{t["expose_border"]}", label="Layer: {_escape_label(layer)}"];'
    )
    lines.append(f"    {tag_domain} -> {product_node} [style=dotted, arrowhead=none];")
    lines.append(f"    {tag_layer} -> {product_node} [style=dotted, arrowhead=none];")
    lines.append("  }")

    # Consumes cluster
    if consume_nodes:
        lines.append("  subgraph cluster_consumes {")
        lines.append('    label="Consumes";')
        lines.append(f'    color="{t["cluster_border"]}";')
        lines.append('    style="rounded,filled";')
        lines.append(f'    fillcolor="{t["cluster_fill"]}";')
        for nid, lbl in consume_nodes:
            lines.append(
                f'    {nid} [shape=folder, style="filled", fillcolor="{t["consume_fill"]}", '
                f'color="{t["consume_border"]}", label="{_escape_label(lbl)}"];'
            )
            lines.append(f"    {nid} -> {product_node};")
        lines.append("  }")

    # Plan cluster (optional)
    if plan_nodes:
        lines.append("  subgraph cluster_plan {")
        lines.append('    label="Build Plan";')
        lines.append(f'    color="{t["cluster_border"]}";')
        lines.append('    style="rounded,filled";')
        lines.append(f'    fillcolor="{t["cluster_fill"]}";')
        first_action_id = None
        last_action_id = None
        for i, (nid, lbl) in enumerate(plan_nodes):
            lines.append(
                f'    {nid} [shape=diamond, style="filled", fillcolor="{t["action_fill"]}", '
                f'color="{t["action_border"]}", label="{_escape_label(lbl)}"];'
            )
            if i == 0:
                first_action_id = nid
            last_action_id = nid
        for a, b in plan_edges:
            lines.append(f"    {a} -> {b} [style=solid, arrowhead=normal];")
        # Link product -> first action if present
        if first_action_id:
            lines.append(f"  {product_node} -> {first_action_id} [style=dashed];")
        lines.append("  }")

    # Exposes cluster
    if expose_nodes:
        lines.append("  subgraph cluster_exposes {")
        lines.append('    label="Exposes";')
        lines.append(f'    color="{t["cluster_border"]}";')
        lines.append('    style="rounded,filled";')
        lines.append(f'    fillcolor="{t["cluster_fill"]}";')
        for nid, lbl in expose_nodes:
            lines.append(
                f'    {nid} [shape=component, style="filled", fillcolor="{t["expose_fill"]}", '
                f'color="{t["expose_border"]}", label="{_escape_label(lbl)}"];'
            )
            # Link last action -> expose if plan exists, else product -> expose
            if plan_nodes:
                last_action_id = plan_nodes[-1][0]
                lines.append(f"    {last_action_id} -> {nid};")
            else:
                lines.append(f"    {product_node} -> {nid};")
        lines.append("  }")

    # Legend (optional)
    if legend:
        lines.append("  subgraph cluster_legend {")
        lines.append('    label="Legend";')
        lines.append(f'    color="{t["legend_border"]}";')
        lines.append('    style="rounded,filled";')
        lines.append(f'    fillcolor="{t["legend_fill"]}";')
        lines.append(
            f'    key_product [shape=box, style="rounded,filled", fillcolor="{t["product_fill"]}", '
            f'color="{t["product_border"]}", label="Data Product"];'
        )
        lines.append(
            f'    key_consume [shape=folder, style="filled", fillcolor="{t["consume_fill"]}", '
            f'color="{t["consume_border"]}", label="Consumed Source"];'
        )
        lines.append(
            f'    key_action [shape=diamond, style="filled", fillcolor="{t["action_fill"]}", '
            f'color="{t["action_border"]}", label="Plan Action"];'
        )
        lines.append(
            f'    key_expose [shape=component, style="filled", fillcolor="{t["expose_fill"]}", '
            f'color="{t["expose_border"]}", label="Exposed Artifact"];'
        )
        lines.append("  }")

    lines.append("}")
    return "\n".join(lines)
