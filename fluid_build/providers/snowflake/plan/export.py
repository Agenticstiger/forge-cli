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

# fluid_build/providers/snowflake/plan/export.py
"""Export FLUID contracts to external formats (OPDS, DOT)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List


def export_opds(src: Mapping[str, Any] | List[Mapping[str, Any]]) -> Dict[str, Any]:
    """
    Export FLUID contract to Open Platform Data Specification (OPDS).

    OPDS provides a cloud-agnostic representation of data platforms.
    """
    contracts = [src] if isinstance(src, Mapping) else src

    opds_doc = {"version": "1.0", "platform": "snowflake", "resources": []}

    for contract in contracts:
        metadata = contract.get("metadata", {})
        binding = contract.get("binding", {})
        location = binding.get("location", {})

        resource = {
            "id": metadata.get("name", "unknown"),
            "type": "dataset",
            "name": metadata.get("name"),
            "description": metadata.get("description"),
            "location": {
                "account": location.get("account"),
                "database": location.get("database"),
                "schema": location.get("schema"),
                "table": location.get("table"),
            },
            "schema": contract.get("schema", {}),
            "tags": metadata.get("tags", []),
        }

        opds_doc["resources"].append(resource)

    return opds_doc


def export_dot_graph(src: Mapping[str, Any] | List[Mapping[str, Any]]) -> Dict[str, Any]:
    """
    Export FLUID contract dependencies as DOT graph format.

    Useful for visualization with Graphviz.
    """
    contracts = [src] if isinstance(src, Mapping) else src

    nodes = []
    edges = []

    for contract in contracts:
        metadata = contract.get("metadata", {})
        name = metadata.get("name", "unknown")

        # Add contract node
        nodes.append(
            {
                "id": name,
                "label": name,
                "type": "dataset",
                "description": metadata.get("description"),
            }
        )

        # Add dependency edges
        dependencies = contract.get("dependencies", [])
        for dep in dependencies:
            edges.append({"from": dep, "to": name, "type": "depends_on"})

    # Generate DOT syntax
    dot_lines = ["digraph FLUID {"]
    dot_lines.append("  rankdir=LR;")
    dot_lines.append("  node [shape=box];")

    for node in nodes:
        dot_lines.append(f'  "{node["id"]}" [label="{node["label"]}"];')

    for edge in edges:
        dot_lines.append(f'  "{edge["from"]}" -> "{edge["to"]}";')

    dot_lines.append("}")

    return {"format": "dot", "graph": "\n".join(dot_lines), "nodes": nodes, "edges": edges}
