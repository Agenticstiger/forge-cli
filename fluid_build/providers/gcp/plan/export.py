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

# fluid_build/providers/gcp/plan/export.py
"""
Export utilities for GCP provider.

Supports exporting FLUID contracts to various formats:
- OPDS (Open Data Product Standard)
- DOT (GraphViz dependency graphs)
- Terraform configurations
"""

import json
from collections.abc import Mapping
from typing import Any, Dict, List, Union


def export_opds(src: Union[Mapping[str, Any], List[Mapping[str, Any]]]) -> Dict[str, Any]:
    """
    Export FLUID contracts to Open Data Product Standard format.

    Args:
        src: FLUID contract or list of contracts

    Returns:
        OPDS-compliant JSON structure
    """
    if isinstance(src, list):
        # Multiple contracts - create OPDS catalog
        return {
            "opds_version": "1.0",
            "kind": "DataProductCatalog",
            "metadata": {
                "generator": "fluid-forge-gcp-provider",
                "generated_at": _utc_timestamp(),
                "count": len(src),
            },
            "data_products": [_contract_to_opds(contract) for contract in src],
        }
    else:
        # Single contract
        return _contract_to_opds(src)


def _contract_to_opds(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Convert single FLUID contract to OPDS format.

    Args:
        contract: FLUID contract specification

    Returns:
        OPDS data product specification
    """
    metadata = contract.get("metadata", {})
    owner_info = metadata.get("owner", {})

    opds = {
        "opds_version": "1.0",
        "kind": "DataProduct",
        "id": contract.get("id"),
        "name": contract.get("name"),
        "domain": metadata.get("domain"),
        "description": contract.get("description", metadata.get("description")),
        # Ownership and governance
        "owner": (
            {
                "name": owner_info.get("team", owner_info.get("name")),
                "email": owner_info.get("email"),
            }
            if owner_info
            else None
        ),
        # Classification
        "classification": {
            "layer": metadata.get("layer"),
            "tags": metadata.get("tags", []),
            "sensitivity": metadata.get("sensitivity"),
        },
        # Data assets
        "assets": [],
        # Quality and SLA information
        "quality": {
            "freshness": metadata.get("freshness"),
            "completeness": metadata.get("completeness"),
            "accuracy": metadata.get("accuracy"),
        },
        # Links and documentation
        "links": [],
        # Additional metadata
        "x-fluid-version": contract.get("fluidVersion"),
        "x-gcp-provider": {
            "generated_at": _utc_timestamp(),
            "version": "1.0",
        },
    }

    # Convert exposures to OPDS assets
    for exposure in contract.get("exposes", []):
        asset = _exposure_to_opds_asset(exposure)
        if asset:
            opds["assets"].append(asset)

    # Add documentation links
    if metadata.get("documentation"):
        opds["links"].append(
            {"rel": "documentation", "href": metadata["documentation"], "title": "Documentation"}
        )

    if metadata.get("repository"):
        opds["links"].append(
            {"rel": "source", "href": metadata["repository"], "title": "Source Code"}
        )

    # Remove None values for cleaner output
    return _remove_none_values(opds)


def _exposure_to_opds_asset(exposure: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Convert FLUID exposure to OPDS asset.

    Args:
        exposure: FLUID exposure specification

    Returns:
        OPDS asset specification
    """
    location = exposure.get("location", {})
    format_type = location.get("format")
    properties = location.get("properties", {})

    asset = {
        "id": exposure.get("id"),
        "name": exposure.get("name", exposure.get("id")),
        "type": exposure.get("type"),
        "description": exposure.get("description"),
        "format": format_type,
    }

    # Add format-specific location information
    if format_type == "bigquery_table":
        asset["location"] = {
            "type": "bigquery_table",
            "project": properties.get("project"),
            "dataset": properties.get("dataset"),
            "table": properties.get("table"),
            "full_name": f"{properties.get('project', '')}.{properties.get('dataset', '')}.{properties.get('table', '')}",
        }

    elif format_type == "bigquery_view":
        asset["location"] = {
            "type": "bigquery_view",
            "project": properties.get("project"),
            "dataset": properties.get("dataset"),
            "view": properties.get("view"),
            "full_name": f"{properties.get('project', '')}.{properties.get('dataset', '')}.{properties.get('view', '')}",
        }

    elif format_type == "gcs_bucket":
        asset["location"] = {
            "type": "gcs_bucket",
            "bucket": properties.get("bucket"),
            "prefix": properties.get("prefix"),
            "full_path": f"gs://{properties.get('bucket', '')}/{properties.get('prefix', '')}",
        }

    elif format_type == "pubsub_topic":
        asset["location"] = {
            "type": "pubsub_topic",
            "project": properties.get("project"),
            "topic": properties.get("topic"),
            "full_name": f"projects/{properties.get('project', '')}/topics/{properties.get('topic', '')}",
        }

    # Add schema information
    schema = exposure.get("schema", [])
    if schema:
        asset["schema"] = {
            "fields": [
                {
                    "name": field.get("name"),
                    "type": field.get("type"),
                    "mode": field.get("mode", "NULLABLE"),
                    "description": field.get("description"),
                }
                for field in schema
            ]
        }

    return _remove_none_values(asset)


def export_dot_graph(src: Union[Mapping[str, Any], List[Mapping[str, Any]]]) -> Dict[str, Any]:
    """
    Export FLUID contracts as GraphViz DOT format.

    Creates dependency graph showing relationships between data products.

    Args:
        src: FLUID contract or list of contracts

    Returns:
        DOT graph specification
    """
    contracts = src if isinstance(src, list) else [src]

    # Build dependency graph
    nodes = []
    edges = []

    for contract in contracts:
        contract_id = contract.get("id", "unknown")

        # Add contract as main node
        nodes.append(
            {
                "id": contract_id,
                "label": contract.get("name", contract_id),
                "type": "data_product",
                "layer": contract.get("metadata", {}).get("layer"),
                "domain": contract.get("metadata", {}).get("domain"),
            }
        )

        # Add exposure nodes
        for exposure in contract.get("exposes", []):
            exposure_id = exposure.get("id", "unknown")
            nodes.append(
                {
                    "id": exposure_id,
                    "label": exposure.get("name", exposure_id),
                    "type": exposure.get("type", "asset"),
                    "format": exposure.get("location", {}).get("format"),
                }
            )

            # Edge from contract to exposure
            edges.append(
                {
                    "from": contract_id,
                    "to": exposure_id,
                    "type": "exposes",
                }
            )

        # Add dependency edges (if dependency information available)
        dependencies = contract.get("dependencies", [])
        for dep in dependencies:
            edges.append(
                {
                    "from": dep.get("id", dep),
                    "to": contract_id,
                    "type": "depends_on",
                }
            )

    # Generate DOT format
    dot_lines = ["digraph FluidDataProducts {"]
    dot_lines.append("  rankdir=LR;")
    dot_lines.append("  node [shape=box, style=rounded];")

    # Add nodes with styling
    for node in nodes:
        node_id = (
            node.get("id", node.get("exposeId", "unknown")).replace(".", "_").replace("-", "_")
        )
        label = node.get("label", node.get("id", node.get("exposeId", "unknown")))
        node_type = node.get("type", node.get("kind", "unknown"))

        # Color by type/kind
        color_map = {
            "data_product": "lightblue",
            "table": "lightgreen",
            "view": "lightyellow",
            "api": "lightcoral",
            "stream": "lightpink",
        }
        color = color_map.get(node_type, "lightgray")

        dot_lines.append(f'  {node_id} [label="{label}", fillcolor={color}, style=filled];')

    # Add edges
    for edge in edges:
        from_id = edge["from"].replace(".", "_").replace("-", "_")
        to_id = edge["to"].replace(".", "_").replace("-", "_")
        edge_type = edge.get("type", "")

        style = "solid" if edge_type == "exposes" else "dashed"
        dot_lines.append(f"  {from_id} -> {to_id} [style={style}];")

    dot_lines.append("}")

    return {
        "format": "dot",
        "content": "\n".join(dot_lines),
        "nodes": len(nodes),
        "edges": len(edges),
        "metadata": {
            "generated_at": _utc_timestamp(),
            "generator": "fluid-forge-gcp-provider",
            "contracts": len(contracts),
        },
    }


def export_terraform(src: Union[Mapping[str, Any], List[Mapping[str, Any]]]) -> Dict[str, Any]:
    """
    Export FLUID contracts as Terraform configuration.

    Generates Terraform HCL for GCP resources.

    Args:
        src: FLUID contract or list of contracts

    Returns:
        Terraform configuration structure
    """
    contracts = src if isinstance(src, list) else [src]

    terraform_config = {
        "terraform": {
            "required_providers": {"google": {"source": "hashicorp/google", "version": "~> 4.0"}}
        },
        "provider": {
            "google": {
                "project": "${var.project_id}",
                "region": "${var.region}",
            }
        },
        "variable": {
            "project_id": {"description": "GCP project ID", "type": "string"},
            "region": {"description": "GCP region", "type": "string", "default": "us-central1"},
        },
        "resource": {},
    }

    # Generate resources for each contract
    for contract in contracts:
        contract_resources = _contract_to_terraform_resources(contract)
        terraform_config["resource"].update(contract_resources)

    return {
        "format": "terraform",
        "content": terraform_config,
        "metadata": {
            "generated_at": _utc_timestamp(),
            "generator": "fluid-forge-gcp-provider",
            "contracts": len(contracts),
        },
    }


def _contract_to_terraform_resources(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Convert FLUID contract to Terraform resources.

    Args:
        contract: FLUID contract specification

    Returns:
        Dictionary of Terraform resource definitions
    """
    resources = {}
    contract_id = contract.get("id", "unknown").replace(".", "_")

    # Create resources for exposures
    for i, exposure in enumerate(contract.get("exposes", [])):
        location = exposure.get("location", {})
        format_type = location.get("format")
        properties = location.get("properties", {})

        if format_type == "bigquery_table":
            dataset_name = properties.get("dataset", "")
            table_name = properties.get("table", "")

            # BigQuery dataset resource
            dataset_resource_name = f"dataset_{contract_id}_{dataset_name}"
            if dataset_resource_name not in resources.get("google_bigquery_dataset", {}):
                resources.setdefault("google_bigquery_dataset", {})[dataset_resource_name] = {
                    "dataset_id": dataset_name,
                    "location": properties.get("location", "US"),
                    "description": f"Dataset for {contract.get('name', contract_id)}",
                    "labels": {
                        "managed_by": "fluid_build",
                        "contract_id": contract_id,
                    },
                }

            # BigQuery table resource
            table_resource_name = f"table_{contract_id}_{table_name}"
            resources.setdefault("google_bigquery_table", {})[table_resource_name] = {
                "dataset_id": f"${{google_bigquery_dataset.{dataset_resource_name}.dataset_id}}",
                "table_id": table_name,
                "description": exposure.get("description", ""),
                "labels": {
                    "managed_by": "fluid_build",
                    "contract_id": contract_id,
                },
            }

            # Add schema if provided
            schema = exposure.get("schema", [])
            if schema:
                schema_json = json.dumps(
                    [
                        {
                            "name": field.get("name"),
                            "type": field.get("type", "STRING"),
                            "mode": field.get("mode", "NULLABLE"),
                            "description": field.get("description", ""),
                        }
                        for field in schema
                    ]
                )
                resources["google_bigquery_table"][table_resource_name]["schema"] = schema_json

    return resources


def _utc_timestamp() -> str:
    """Generate UTC timestamp string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remove_none_values(obj: Any) -> Any:
    """Recursively remove None values from dictionaries and lists."""
    if isinstance(obj, dict):
        return {k: _remove_none_values(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, list):
        return [_remove_none_values(v) for v in obj if v is not None]
    else:
        return obj
