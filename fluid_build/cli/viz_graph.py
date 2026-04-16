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

# fluid_build/cli/commands/viz_graph.py
"""
FLUID Build Visualization Graph Command

A production-ready CLI command for generating beautiful, interactive data product
lineage and build graphs from FLUID contracts. Supports multiple output formats,
themes, and advanced visualization features.

Features:
- Multiple output formats (DOT, SVG, PNG, HTML)
- Dark/light themes with customizable colors
- Plan integration showing build actions
- Interactive HTML output with metadata
- Robust error handling and validation
- Performance optimizations for large graphs
- Extensible theme system
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ._common import CLIError, load_contract_with_overlay
from ._logging import error, info, warn
from .security import (
    ProductionLogger,
    read_file_secure,
    validate_input_file,
    validate_output_file,
)

COMMAND = "viz-graph"  # keep name consistent (you can add aliases in bootstrap)

# Version and metadata
__version__ = "2.0.0"
__author__ = "FLUID Team"

# --------------------------- Configuration & Data Classes --------------------------- #


@dataclass
class GraphConfig:
    """Configuration class for graph generation with validation."""

    # Input/Output
    contract_path: str
    output_path: str = "runtime/graph/contract.svg"
    format: str = "svg"
    environment: Optional[str] = None
    plan_path: Optional[str] = None

    # Layout & Appearance
    theme: str = "dark"
    rankdir: str = "LR"
    title: Optional[str] = None
    show_legend: bool = False

    # Graph Content
    collapse_consumes: bool = False
    collapse_exposes: bool = False
    show_metadata: bool = True
    show_descriptions: bool = False
    max_label_length: int = 50

    # Behavior
    open_when_done: bool = False
    force_overwrite: bool = False
    quiet: bool = False

    # Advanced
    custom_theme_path: Optional[str] = None
    graphviz_args: List[str] = field(default_factory=list)

    def __post_init__(self):
        """Validate configuration after initialization."""
        self.validate()

    def validate(self) -> None:
        """Validate configuration parameters."""
        # Format validation
        valid_formats = {"dot", "svg", "png", "html"}
        if self.format not in valid_formats:
            raise ValueError(f"Invalid format '{self.format}'. Must be one of: {valid_formats}")

        # Theme validation
        valid_themes = set(THEMES.keys())
        if self.theme not in valid_themes and not self.custom_theme_path:
            raise ValueError(f"Invalid theme '{self.theme}'. Must be one of: {valid_themes}")

        # Rankdir validation
        valid_rankdirs = {"LR", "TB", "RL", "BT"}
        if self.rankdir not in valid_rankdirs:
            raise ValueError(f"Invalid rankdir '{self.rankdir}'. Must be one of: {valid_rankdirs}")

        # Path validation
        if not Path(self.contract_path).exists():
            raise FileNotFoundError(f"Contract file not found: {self.contract_path}")

        if self.plan_path and not Path(self.plan_path).exists():
            raise FileNotFoundError(f"Plan file not found: {self.plan_path}")

        if self.custom_theme_path and not Path(self.custom_theme_path).exists():
            raise FileNotFoundError(f"Custom theme file not found: {self.custom_theme_path}")

        # Label length validation
        if self.max_label_length < 10:
            raise ValueError("max_label_length must be at least 10 characters")


@dataclass
class GraphMetrics:
    """Track graph generation metrics for performance monitoring."""

    start_time: float = field(default_factory=time.time)
    load_time: Optional[float] = None
    render_time: Optional[float] = None
    total_time: Optional[float] = None

    # Graph statistics
    node_count: int = 0
    edge_count: int = 0
    cluster_count: int = 0

    # File statistics
    input_size: Optional[int] = None
    output_size: Optional[int] = None
    dot_size: Optional[int] = None

    def mark_load_complete(self) -> None:
        """Mark contract loading as complete."""
        self.load_time = time.time() - self.start_time

    def mark_render_complete(self) -> None:
        """Mark rendering as complete."""
        current_time = time.time()
        if self.load_time is not None:
            self.render_time = current_time - (self.start_time + self.load_time)
        self.total_time = current_time - self.start_time

    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary for logging."""
        return {
            "load_time_ms": round((self.load_time or 0) * 1000, 2),
            "render_time_ms": round((self.render_time or 0) * 1000, 2),
            "total_time_ms": round((self.total_time or 0) * 1000, 2),
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "cluster_count": self.cluster_count,
            "input_size_bytes": self.input_size,
            "output_size_bytes": self.output_size,
            "dot_size_bytes": self.dot_size,
        }


# --------------------------- Enhanced Theming & Styles --------------------------- #

THEMES = {
    "dark": {
        "bg": "#0B1020",
        "fg": "#E5E7EB",
        "grid": "#22324e",
        "cluster_border": "#425c9e",
        "cluster_fill": "#10182f",
        "product_fill": "#172554",
        "product_border": "#93C5FD",
        "consume_fill": "#052e1a",
        "consume_border": "#34D399",
        "expose_fill": "#1F2937",
        "expose_border": "#94A3B8",
        "action_fill": "#3C2A4D",
        "action_border": "#E879F9",
        "legend_fill": "#0f172a",
        "legend_border": "#6068a6",
        "edge": "#8FA8D6",
        "edge_highlight": "#60A5FA",
        "font": "Inter",
        "node_spacing": "0.5",
        "rank_spacing": "1.0",
    },
    "light": {
        "bg": "#ffffff",
        "fg": "#111827",
        "grid": "#d8e0f7",
        "cluster_border": "#8da1d7",
        "cluster_fill": "#f6f8ff",
        "product_fill": "#e3ecff",
        "product_border": "#1e40af",
        "consume_fill": "#eaffe8",
        "consume_border": "#047857",
        "expose_fill": "#eef2f7",
        "expose_border": "#475569",
        "action_fill": "#f8e5ff",
        "action_border": "#7e22ce",
        "legend_fill": "#eef2ff",
        "legend_border": "#6366f1",
        "edge": "#4b5563",
        "edge_highlight": "#2563EB",
        "font": "Inter",
        "node_spacing": "0.5",
        "rank_spacing": "1.0",
    },
    "minimal": {
        "bg": "#fafafa",
        "fg": "#333333",
        "grid": "#e5e5e5",
        "cluster_border": "#999999",
        "cluster_fill": "#ffffff",
        "product_fill": "#f0f0f0",
        "product_border": "#666666",
        "consume_fill": "#f8f8f8",
        "consume_border": "#888888",
        "expose_fill": "#f8f8f8",
        "expose_border": "#888888",
        "action_fill": "#f5f5f5",
        "action_border": "#777777",
        "legend_fill": "#ffffff",
        "legend_border": "#cccccc",
        "edge": "#666666",
        "edge_highlight": "#333333",
        "font": "Arial",
        "node_spacing": "0.3",
        "rank_spacing": "0.8",
    },
    "blueprint": {
        "bg": "#1e3a8a",
        "fg": "#ffffff",
        "grid": "#3b82f6",
        "cluster_border": "#60a5fa",
        "cluster_fill": "#1e40af",
        "product_fill": "#2563eb",
        "product_border": "#93c5fd",
        "consume_fill": "#1d4ed8",
        "consume_border": "#dbeafe",
        "expose_fill": "#1e40af",
        "expose_border": "#bfdbfe",
        "action_fill": "#3730a3",
        "action_border": "#c7d2fe",
        "legend_fill": "#1e40af",
        "legend_border": "#93c5fd",
        "edge": "#bfdbfe",
        "edge_highlight": "#ffffff",
        "font": "Courier New",
        "node_spacing": "0.6",
        "rank_spacing": "1.2",
    },
}


# --------------------------- Enhanced Helper Functions --------------------------- #


def _safe_id(s: str) -> str:
    """Generate a safe GraphViz identifier from a string."""
    return (
        s.replace("-", "_")
        .replace(".", "_")
        .replace("/", "_")
        .replace(" ", "_")
        .replace(":", "_")
        .replace("@", "_")
        .replace("#", "_")
        .replace("%", "_")
        .replace("&", "_")
    )


def _escape_label(s: str, max_length: Optional[int] = None) -> str:
    """Escape and optionally truncate a label for GraphViz."""
    if max_length and len(s) > max_length:
        s = s[: max_length - 3] + "..."
    return s.replace('"', '\\"').replace("\n", "\\n").replace("\r", "").replace("\t", " ")


def _get_theme_value(theme: str, key: str, custom_theme: Optional[Dict[str, str]] = None) -> str:
    """Get a theme value with fallback to default theme."""
    if custom_theme and key in custom_theme:
        return custom_theme[key]
    return THEMES.get(theme, THEMES["dark"]).get(key, THEMES["dark"][key])


def _load_custom_theme(theme_path: str) -> Dict[str, str]:
    """Securely load a custom theme from a JSON or YAML file."""
    try:
        content = read_file_secure(theme_path, "custom theme file")

        path = Path(theme_path)
        if path.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            return yaml.safe_load(content)
        else:
            return json.loads(content)
    except Exception as e:
        raise ValueError(f"Failed to parse custom theme file {theme_path}: {e}")


def _shell_open(path: Path, logger: logging.Logger) -> None:
    """Securely open a file with the system's default application."""
    # Validate path first
    try:
        validated_path = validate_input_file(path, "output file")
    except Exception as e:
        warn(logger, "invalid_file_for_opening", file=str(path), error=str(e))
        return

    secure_logger = ProductionLogger(logger)

    try:
        system = platform.system()

        if system == "Darwin":
            subprocess.run(
                ["open", str(validated_path)], check=True, capture_output=True, timeout=10
            )
        elif system == "Windows":
            os.startfile(str(validated_path))  # type: ignore[attr-defined]
        else:
            subprocess.run(
                ["xdg-open", str(validated_path)], check=True, capture_output=True, timeout=10
            )

        secure_logger.log_safe("info", f"Opened file in system viewer: {validated_path}")

    except subprocess.TimeoutExpired:
        warn(logger, "file_open_timeout", file=str(path))
    except subprocess.CalledProcessError as e:
        warn(logger, "failed_to_open_file", file=str(path), error=f"Command failed: {e}")
    except Exception as e:
        secure_logger.log_safe("warning", f"Failed to open file: {validated_path}", error=str(e))


def _check_graphviz_installation() -> Tuple[bool, Optional[str]]:
    """Securely check if Graphviz is installed and return version info."""
    if not shutil.which("dot"):
        return False, None

    try:
        result = subprocess.run(
            ["dot", "-V"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        # Graphviz outputs version to stderr
        if result and result.stderr:
            version_output = result.stderr.strip()
            return True, version_output
        else:
            return True, "Unknown version"

    except Exception:
        return False, None


def _validate_input_file(path: str, file_type: str = "contract") -> Path:
    """Validate that an input file exists and is readable - uses secure validation."""
    try:
        return validate_input_file(path, file_type)
    except Exception as e:
        # Convert security exceptions to appropriate CLI exceptions
        if "not found" in str(e).lower():
            raise FileNotFoundError(f"{file_type.title()} file not found: {path}")
        elif "permission" in str(e).lower():
            raise PermissionError(f"Permission denied reading {file_type} file: {path}")
        elif "extension" in str(e).lower() or "path" in str(e).lower():
            raise ValueError(f"Invalid {file_type} file: {path} - {str(e)}")
        else:
            raise ValueError(f"{file_type.title()} file validation failed: {path} - {str(e)}")


def _prepare_output_directory(output_path: str, force_overwrite: bool = False) -> Path:
    """Prepare the output directory and validate output path - uses secure validation."""
    try:
        out_path = validate_output_file(output_path, "graph output")

        # Check if output file already exists
        if out_path.exists() and not force_overwrite:
            raise FileExistsError(
                f"Output file already exists: {output_path}. "
                "Use --force to overwrite or choose a different output path."
            )

        return out_path
    except Exception as e:
        # Convert security exceptions to appropriate CLI exceptions
        if "permission" in str(e).lower():
            raise PermissionError(f"Permission denied writing to output path: {output_path}")
        elif "path" in str(e).lower() or "forbidden" in str(e).lower():
            raise ValueError(f"Invalid output path: {output_path} - {str(e)}")
        else:
            raise ValueError(f"Output path validation failed: {output_path} - {str(e)}")


def _read_plan(path: Optional[str]) -> Optional[Mapping[str, Any]]:
    """Securely read and parse a plan JSON file."""
    if not path:
        return None

    try:
        content = read_file_secure(path, "plan file")
        return json.loads(content)
    except Exception:
        # Return None for plan files that don't exist or can't be parsed
        # This is non-fatal since plan is optional
        return None


def _get_file_size(path: Union[str, Path]) -> Optional[int]:
    """Get file size in bytes, return None if file doesn't exist."""
    try:
        return Path(path).stat().st_size
    except (FileNotFoundError, OSError):
        return None


# --------------------------- Enhanced DOT Graph Builder --------------------------- #


class GraphBuilder:
    """Enhanced graph builder with improved organization and features."""

    def __init__(self, config: GraphConfig, metrics: GraphMetrics, logger: logging.Logger):
        self.config = config
        self.metrics = metrics
        self.logger = logger
        self.custom_theme = None

        # Load custom theme if specified
        if config.custom_theme_path:
            self.custom_theme = _load_custom_theme(config.custom_theme_path)

    def _get_theme_value(self, key: str) -> str:
        """Get a theme value with custom theme support."""
        return _get_theme_value(self.config.theme, key, self.custom_theme)

    def _build_product_cluster(self, contract: Mapping[str, Any], product_node: str) -> List[str]:
        """Build the main product cluster."""
        lines = []
        t = self._get_theme_value

        c_id = contract.get("id", "product")
        c_name = contract.get("name") or c_id
        meta = contract.get("metadata") or {}
        domain = contract.get("domain", "Unknown")
        layer = meta.get("layer", "N/A")

        lines.append("  subgraph cluster_product {")
        lines.append('    label="Data Product";')
        lines.append(f'    color="{t("cluster_border")}";')
        lines.append('    style="rounded,filled";')
        lines.append(f'    fillcolor="{t("cluster_fill")}";')

        # Product node
        product_label = f"{_escape_label(c_name, self.config.max_label_length)}"
        if self.config.show_metadata:
            product_label += f"\\n({_escape_label(c_id)})"

        lines.append(
            f'    {product_node} [shape=box, style="rounded,filled", '
            f'fillcolor="{t("product_fill")}", color="{t("product_border")}", '
            f'penwidth=2, label="{product_label}"];'
        )

        # Metadata tags
        if self.config.show_metadata:
            tag_domain = _safe_id(f"tag_domain_{domain}")
            tag_layer = _safe_id(f"tag_layer_{layer}")

            lines.append(
                f'    {tag_domain} [shape=note, style="filled", fontsize=10, '
                f'fillcolor="{t("expose_fill")}", color="{t("expose_border")}", '
                f'label="Domain: {_escape_label(domain)}"];'
            )
            lines.append(
                f'    {tag_layer} [shape=note, style="filled", fontsize=10, '
                f'fillcolor="{t("expose_fill")}", color="{t("expose_border")}", '
                f'label="Layer: {_escape_label(layer)}"];'
            )
            lines.append(f"    {tag_domain} -> {product_node} [style=dotted, arrowhead=none];")
            lines.append(f"    {tag_layer} -> {product_node} [style=dotted, arrowhead=none];")

        lines.append("  }")
        return lines

    def _build_consumes_cluster(
        self, consumes: Sequence[Mapping[str, Any]], product_node: str
    ) -> Tuple[List[str], List[Tuple[str, str]]]:
        """Build the consumes cluster and return lines and node info."""
        if not consumes:
            return [], []

        lines = []
        consume_nodes = []
        t = self._get_theme_value

        if self.config.collapse_consumes:
            consume_nodes.append(("consumes_agg", "Consumes…"))
        else:
            for c in consumes:
                rid = str(c.get("ref") or c.get("id") or "source")
                nid = _safe_id(f"consume_{rid}")

                # Build label
                lbl_parts = [c.get("id") or "source"]
                if not self.config.collapse_consumes:
                    lbl_parts.append(rid)
                if self.config.show_descriptions and c.get("description"):
                    lbl_parts.append(f"({c['description']})")

                label = "\\n".join(lbl_parts)
                consume_nodes.append((nid, label))

        if consume_nodes:
            lines.append("  subgraph cluster_consumes {")
            lines.append('    label="Consumes";')
            lines.append(f'    color="{t("cluster_border")}";')
            lines.append('    style="rounded,filled";')
            lines.append(f'    fillcolor="{t("cluster_fill")}";')

            for nid, lbl in consume_nodes:
                escaped_label = _escape_label(lbl, self.config.max_label_length)
                lines.append(
                    f'    {nid} [shape=folder, style="filled", '
                    f'fillcolor="{t("consume_fill")}", color="{t("consume_border")}", '
                    f'label="{escaped_label}"];'
                )
                lines.append(f"    {nid} -> {product_node};")

            lines.append("  }")
            self.metrics.node_count += len(consume_nodes)
            self.metrics.edge_count += len(consume_nodes)

        return lines, consume_nodes

    def _build_exposes_cluster(
        self, exposes: Sequence[Mapping[str, Any]], product_node: str
    ) -> Tuple[List[str], List[Tuple[str, str]]]:
        """Build the exposes cluster and return lines and node info."""
        if not exposes:
            return [], []

        lines = []
        expose_nodes = []
        t = self._get_theme_value

        if self.config.collapse_exposes:
            expose_nodes.append(("exposes_agg", "Exposes…"))
        else:
            for e in exposes:
                eid = str(e.get("id") or "expose")
                et = str(e.get("type") or "")
                loc = e.get("location") or {}
                fmt = loc.get("format") or ""
                nid = _safe_id(f"expose_{eid}")

                # Build label
                lbl_parts = [eid]
                if et:
                    lbl_parts.append(f"{et} {f'[{fmt}]' if fmt else ''}".strip())
                if self.config.show_descriptions and e.get("description"):
                    lbl_parts.append(f"({e['description']})")

                label = "\\n".join(lbl_parts)
                expose_nodes.append((nid, label))

        if expose_nodes:
            lines.append("  subgraph cluster_exposes {")
            lines.append('    label="Exposes";')
            lines.append(f'    color="{t("cluster_border")}";')
            lines.append('    style="rounded,filled";')
            lines.append(f'    fillcolor="{t("cluster_fill")}";')

            for nid, lbl in expose_nodes:
                escaped_label = _escape_label(lbl, self.config.max_label_length)
                lines.append(
                    f'    {nid} [shape=component, style="filled", '
                    f'fillcolor="{t("expose_fill")}", color="{t("expose_border")}", '
                    f'label="{escaped_label}"];'
                )
                lines.append(f"    {product_node} -> {nid};")

            lines.append("  }")
            self.metrics.node_count += len(expose_nodes)
            self.metrics.edge_count += len(expose_nodes)

        return lines, expose_nodes

    def build_dot(
        self, contract: Mapping[str, Any], plan: Optional[Mapping[str, Any]] = None
    ) -> str:
        """Build the complete DOT graph."""
        lines = []
        t = self._get_theme_value

        # Basic contract info
        c_id = contract.get("id", "product")
        c_name = contract.get("name") or c_id
        meta = contract.get("metadata") or {}
        domain = contract.get("domain", "Unknown")
        layer = meta.get("layer", "N/A")

        consumes: Sequence[Mapping[str, Any]] = contract.get("consumes") or []
        exposes: Sequence[Mapping[str, Any]] = contract.get("exposes") or []

        # Graph header
        lines.append("digraph G {")
        lines.append(
            f'  graph [bgcolor="{t("bg")}", color="{t("grid")}", fontname="{t("font")}", labeljust="l"];'
        )
        lines.append(f'  node [fontname="{t("font")}", color="{t("fg")}", fontcolor="{t("fg")}"];')
        lines.append(f'  edge [color="{t("edge")}", arrowsize=0.8];')
        lines.append(f"  rankdir={self.config.rankdir};")

        # Title
        graph_title = self.config.title or f"{c_name}  •  Domain: {domain}  •  Layer: {layer}"
        lines.append('  labelloc="t";')
        lines.append(f'  label="{_escape_label(graph_title)}";')

        # Main product node
        product_node = _safe_id(f"product_{c_id}")
        self.metrics.node_count += 1

        # Build product cluster
        product_lines = self._build_product_cluster(contract, product_node)
        lines.extend(product_lines)
        self.metrics.cluster_count += 1

        # Build consumes cluster
        consumes_lines, consume_nodes = self._build_consumes_cluster(consumes, product_node)
        lines.extend(consumes_lines)
        if consume_nodes:
            self.metrics.cluster_count += 1

        # Build exposes cluster
        exposes_lines, expose_nodes = self._build_exposes_cluster(exposes, product_node)
        lines.extend(exposes_lines)
        if expose_nodes:
            self.metrics.cluster_count += 1

        # Build plan cluster if plan provided
        if plan and isinstance(plan.get("actions"), list) and plan["actions"]:
            plan_lines = self._build_plan_cluster(plan["actions"], product_node, expose_nodes)
            lines.extend(plan_lines)
            self.metrics.cluster_count += 1

        lines.append("}")
        return "\n".join(lines)

    def _build_plan_cluster(
        self,
        actions: List[Mapping[str, Any]],
        product_node: str,
        expose_nodes: List[Tuple[str, str]],
    ) -> List[str]:
        """Build the plan cluster for build actions."""
        if not actions:
            return []

        lines = []
        t = self._get_theme_value

        lines.append("  subgraph cluster_plan {")
        lines.append('    label="Build Plan";')
        lines.append(f'    color="{t("cluster_border")}";')
        lines.append('    style="rounded,filled";')
        lines.append(f'    fillcolor="{t("cluster_fill")}";')

        prev_action_id = None
        first_action_id = None
        last_action_id = None

        for i, action in enumerate(actions):
            op = str(action.get("op", "action"))
            nid = _safe_id(f"action_{i}_{op}")

            # Build label
            label = op
            if "dataset" in action and "table" in action:
                label = f"{op}\\n{action['dataset']}.{action['table']}"
            elif "name" in action:
                label = f"{op}\\n{action['name']}"
            elif "dst" in action:
                label = f"{op}\\n{action['dst']}"

            lines.append(
                f'    {nid} [shape=diamond, style="filled", '
                f'fillcolor="{t("action_fill")}", color="{t("action_border")}", '
                f'label="{_escape_label(label)}"];'
            )

            # Connect actions in sequence
            if prev_action_id:
                lines.append(f"    {prev_action_id} -> {nid} [style=solid, arrowhead=normal];")
            else:
                first_action_id = nid

            prev_action_id = nid
            last_action_id = nid

        lines.append("  }")

        # Connect product to first action and last action to exposes
        if first_action_id:
            lines.append(f"  {product_node} -> {first_action_id} [style=dashed];")

        if last_action_id and expose_nodes:
            for expose_id, _ in expose_nodes:
                lines.append(f"  {last_action_id} -> {expose_id} [style=dashed];")

        self.metrics.node_count += len(actions)
        self.metrics.edge_count += len(actions) - 1  # n-1 internal edges
        if first_action_id:
            self.metrics.edge_count += 1  # product -> first action
        if last_action_id and expose_nodes:
            self.metrics.edge_count += len(expose_nodes)  # last action -> exposes

        return lines


# --------------------------- Legacy DOT Builders (for backward compatibility) --------------------------- #


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


def _write_output(
    dot: str,
    config: GraphConfig,
    metrics: GraphMetrics,
    logger: logging.Logger,
) -> None:
    """Enhanced output writer with security hardening and better error handling."""
    secure_logger = ProductionLogger(logger)

    try:
        out_path = _prepare_output_directory(config.output_path, config.force_overwrite)

        # Track input metrics
        metrics.dot_size = len(dot.encode("utf-8"))

        # If DOT requested or Graphviz not available, write DOT
        graphviz_available, graphviz_version = _check_graphviz_installation()

        if config.format == "dot" or not graphviz_available:
            if config.format != "dot" and not graphviz_available:
                warn(
                    logger,
                    "graphviz_not_available_writing_dot",
                    out=str(out_path.with_suffix(".dot")),
                )
                out_path = out_path.with_suffix(".dot")

            # Secure file write
            from .security import write_file_secure

            write_file_secure(out_path, dot, "DOT graph file")
            metrics.output_size = _get_file_size(out_path)

            if not config.quiet:
                info(
                    logger,
                    "viz_graph_output_written",
                    out=str(out_path),
                    fmt="dot",
                    size_bytes=metrics.output_size,
                )

            if config.open_when_done:
                _shell_open(out_path, logger)
            return

        # Render using Graphviz with security safeguards
        fmt_map = {"svg": "svg", "png": "png", "html": "svg"}
        gv_fmt = fmt_map.get(config.format, "svg")

        # Validate format to prevent injection
        if not gv_fmt.isalnum():
            raise ValueError(f"Invalid Graphviz format: {gv_fmt}")

        # Use secure temporary file for DOT input
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".dot", delete=False, encoding="utf-8"
        ) as tmp_dot:
            tmp_dot.write(dot)
            tmp_dot_path = tmp_dot.name

        try:
            result_file = out_path if config.format != "html" else out_path.with_suffix(".svg")

            # Build Graphviz command with input validation
            cmd = ["dot", f"-T{gv_fmt}", tmp_dot_path, "-o", str(result_file)]

            # Validate and sanitize custom Graphviz args
            if config.graphviz_args:
                sanitized_args = []
                for arg in config.graphviz_args:
                    # Allow only safe Graphviz options
                    if (
                        arg.startswith("-")
                        and len(arg) > 1
                        and arg[1:].replace("=", "").replace(":", "").isalnum()
                    ):
                        sanitized_args.append(arg)
                    else:
                        secure_logger.log_safe(
                            "warning", f"Skipping potentially unsafe Graphviz argument: {arg}"
                        )

                if sanitized_args:
                    # Insert custom args before the -o option
                    cmd = cmd[:-2] + sanitized_args + cmd[-2:]

            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=Path.cwd(),
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )

            if config.format == "html":
                # Wrap SVG into HTML with secure file operations
                from .security import read_file_secure, write_file_secure

                svg_content = read_file_secure(result_file, "generated SVG")
                html_content = _create_html_wrapper(svg_content, config, metrics)
                write_file_secure(out_path, html_content, "HTML wrapper")
                result_file.unlink(missing_ok=True)

            metrics.output_size = _get_file_size(out_path)

            if not config.quiet:
                info(
                    logger,
                    "viz_graph_output_written",
                    out=str(out_path),
                    fmt=config.format,
                    size_bytes=metrics.output_size,
                    graphviz_version=graphviz_version,
                )

            if config.open_when_done:
                _shell_open(out_path, logger)

        except subprocess.TimeoutExpired:
            error(logger, "graphviz_timeout", timeout_seconds=30)
            raise CLIError(1, "graphviz_timeout", {"timeout": 30})
        except subprocess.CalledProcessError as e:
            secure_logger.log_safe(
                "error", "Graphviz render failed", stderr=e.stderr, returncode=e.returncode
            )
            # Fallback: write DOT file
            from .security import write_file_secure

            dot_path = out_path.with_suffix(".dot")
            write_file_secure(dot_path, dot, "fallback DOT file")
            warn(logger, "falling_back_to_dot_output", out=str(dot_path))
            if config.open_when_done:
                _shell_open(dot_path, logger)
        finally:
            # Clean up temporary file securely
            try:
                temp_path = Path(tmp_dot_path)
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    except Exception as e:
        secure_logger.log_safe(
            "error", "Output write failed", error=str(e), output_path=config.output_path
        )
        raise CLIError(1, "output_write_failed", {"error": str(e), "path": config.output_path})


def _create_html_wrapper(svg_content: str, config: GraphConfig, metrics: GraphMetrics) -> str:
    """Create an enhanced HTML wrapper for SVG content."""
    metadata_info = ""
    if config.show_metadata:
        # Handle potential None values in metrics
        total_time = metrics.total_time or 0
        _load_time = metrics.load_time or 0  # noqa: F841
        _render_time = metrics.render_time or 0  # noqa: F841

        metadata_info = f"""
        <div class="metadata">
            <h2>Generation Info</h2>
            <div class="meta-grid">
                <div class="meta-item">
                    <span class="meta-label">Total Time:</span>
                    <span class="meta-value">{total_time:.2f}s</span>
                </div>
                <div class="meta-item">
                    <span class="meta-label">Nodes:</span>
                    <span class="meta-value">{metrics.node_count}</span>
                </div>
                <div class="meta-item">
                    <span class="meta-label">Edges:</span>
                    <span class="meta-value">{metrics.edge_count}</span>
                </div>
                <div class="meta-item">
                    <span class="meta-label">Clusters:</span>
                    <span class="meta-value">{metrics.cluster_count}</span>
                </div>
                <div class="meta-item">
                    <span class="meta-label">Theme:</span>
                    <span class="meta-value">{config.theme}</span>
                </div>
                <div class="meta-item">
                    <span class="meta-label">Layout:</span>
                    <span class="meta-value">{config.rankdir}</span>
                </div>
            </div>
        </div>
        """

    theme_bg = _get_theme_value(config.theme, "bg", None)
    theme_fg = _get_theme_value(config.theme, "fg", None)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FLUID Data Product Graph</title>
    <style>
        body {{ 
            margin: 0; 
            padding: 0;
            background: {theme_bg}; 
            color: {theme_fg}; 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', sans-serif;
            line-height: 1.5;
        }}
        .container {{ 
            padding: 20px; 
            max-width: 100%;
            overflow-x: auto;
        }}
        .header {{
            margin-bottom: 20px;
            padding: 20px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        .header h1 {{ 
            margin: 0 0 8px 0; 
            font-size: 24px; 
            font-weight: 600;
        }}
        .header .subtitle {{ 
            color: rgba(255, 255, 255, 0.7); 
            font-size: 14px; 
        }}
        .graph-container {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            overflow: auto;
        }}
        .metadata {{
            margin-top: 20px;
            padding: 16px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 8px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        .metadata h2 {{
            margin: 0 0 12px 0;
            font-size: 16px;
            font-weight: 600;
        }}
        .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 8px;
        }}
        .meta-item {{
            display: flex;
            justify-content: space-between;
            padding: 4px 0;
        }}
        .meta-label {{
            font-weight: 500;
            opacity: 0.8;
        }}
        .meta-value {{
            font-family: 'SF Mono', 'Monaco', 'Courier New', monospace;
            font-size: 13px;
        }}
        svg {{ 
            width: 100%; 
            height: auto; 
            max-width: none;
        }}
        @media (max-width: 768px) {{
            .container {{ padding: 12px; }}
            .header {{ padding: 16px; }}
            .graph-container {{ padding: 12px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>FLUID Data Product Graph</h1>
            <div class="subtitle">Generated by fluid viz-graph • {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</div>
        </div>
        <div class="graph-container">
            {svg_content}
        </div>
        {metadata_info}
    </div>
</body>
</html>"""


# --------------------------- Enhanced CLI Registration & Runner --------------------------- #


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the enhanced viz-graph command with comprehensive options."""
    p = subparsers.add_parser(
        COMMAND,
        help="Generate beautiful, interactive data product lineage and build graphs",
        description="""
        Create stunning visualizations of your FLUID data product contracts with advanced
        theming, interactive features, and multiple output formats. Perfect for documentation,
        presentations, and understanding complex data lineages.
        """,
        epilog=f"""
        Examples:
          # Basic graph generation
          {COMMAND} contract.fluid.yaml
          {COMMAND} my-data-product/contract.fluid.yaml
          
          # Advanced formatting and theming
          {COMMAND} contract.fluid.yaml --format html --theme dark --open
          {COMMAND} contract.fluid.yaml --theme blueprint --show-legend
          
          # With execution plan integration
          {COMMAND} contract.fluid.yaml --plan runtime/plan.json
          
          # Custom output and styling
          {COMMAND} contract.fluid.yaml --out docs/graph.svg --title "Customer Churn Pipeline"
          
        Version: {__version__}
        Author: {__author__}
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Input/Output arguments
    input_group = p.add_argument_group("Input/Output")
    input_group.add_argument("contract", help="Path to contract.fluid.yaml file")
    input_group.add_argument("--env", help="Environment overlay for contract (e.g., dev, prod)")
    input_group.add_argument("--plan", help="Optional plan.json file to show build actions")
    input_group.add_argument(
        "--out",
        "--output",
        dest="output_path",
        default="runtime/graph/contract.svg",
        help="Output file path (default: runtime/graph/contract.svg)",
    )

    # Format and appearance
    format_group = p.add_argument_group("Format & Appearance")
    format_group.add_argument(
        "--format",
        choices=["dot", "svg", "png", "html"],
        default="svg",
        help="Output format (default: svg)",
    )
    format_group.add_argument(
        "--theme", choices=list(THEMES.keys()), default="dark", help="Color theme (default: dark)"
    )
    format_group.add_argument(
        "--custom-theme", dest="custom_theme_path", help="Path to custom theme JSON/YAML file"
    )
    format_group.add_argument(
        "--rankdir",
        choices=["LR", "TB", "RL", "BT"],
        default="LR",
        help="Graph layout direction (default: LR)",
    )
    format_group.add_argument("--title", help="Custom title for the graph")

    # Content options
    content_group = p.add_argument_group("Content Options")
    content_group.add_argument(
        "--show-legend", action="store_true", help="Add a legend explaining node types"
    )
    content_group.add_argument(
        "--collapse-consumes",
        action="store_true",
        help="Collapse all consumed sources into one node",
    )
    content_group.add_argument(
        "--collapse-exposes",
        action="store_true",
        help="Collapse all exposed artifacts into one node",
    )
    content_group.add_argument(
        "--show-descriptions", action="store_true", help="Include descriptions in node labels"
    )
    content_group.add_argument(
        "--hide-metadata", action="store_true", help="Hide domain/layer metadata tags"
    )
    content_group.add_argument(
        "--max-label-length",
        type=int,
        default=50,
        help="Maximum label length before truncation (default: 50)",
    )

    # Behavior options
    behavior_group = p.add_argument_group("Behavior")
    behavior_group.add_argument(
        "--open",
        dest="open_when_done",
        action="store_true",
        help="Open output file in default viewer when done",
    )
    behavior_group.add_argument(
        "--force",
        dest="force_overwrite",
        action="store_true",
        help="Overwrite existing output file without prompting",
    )
    behavior_group.add_argument("--quiet", action="store_true", help="Suppress non-error output")

    # Advanced options
    advanced_group = p.add_argument_group("Advanced")
    advanced_group.add_argument(
        "--graphviz-args",
        nargs="*",
        default=[],
        help="Additional arguments to pass to Graphviz dot command",
    )
    advanced_group.add_argument(
        "--debug", action="store_true", help="Enable debug output and save intermediate files"
    )

    p.set_defaults(cmd=COMMAND, func=run)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Enhanced main handler for viz-graph command."""
    time.time()

    try:
        # Create configuration from args
        config = GraphConfig(
            contract_path=args.contract,
            output_path=getattr(args, "output_path", "runtime/graph/contract.svg"),
            format=getattr(args, "format", "svg"),
            environment=getattr(args, "env", None),
            plan_path=getattr(args, "plan", None),
            theme=getattr(args, "theme", "dark"),
            rankdir=getattr(args, "rankdir", "LR"),
            title=getattr(args, "title", None),
            show_legend=bool(getattr(args, "show_legend", False)),
            collapse_consumes=bool(getattr(args, "collapse_consumes", False)),
            collapse_exposes=bool(getattr(args, "collapse_exposes", False)),
            show_metadata=not bool(getattr(args, "hide_metadata", False)),
            show_descriptions=bool(getattr(args, "show_descriptions", False)),
            max_label_length=getattr(args, "max_label_length", 50),
            open_when_done=bool(getattr(args, "open_when_done", False)),
            force_overwrite=bool(getattr(args, "force_overwrite", False)),
            quiet=bool(getattr(args, "quiet", False)),
            custom_theme_path=getattr(args, "custom_theme_path", None),
            graphviz_args=getattr(args, "graphviz_args", []),
        )

        # Initialize metrics
        metrics = GraphMetrics()

        if not config.quiet:
            info(
                logger,
                "viz_graph_starting",
                contract=config.contract_path,
                format=config.format,
                theme=config.theme,
            )

        # Check Graphviz availability early if needed
        if config.format != "dot":
            available, version = _check_graphviz_installation()
            if not available:
                warn(logger, "graphviz_not_available", note="Will output DOT format instead")

        # Load contract and plan
        contract = load_contract_with_overlay(config.contract_path, config.environment, logger)
        metrics.input_size = _get_file_size(config.contract_path)
        metrics.mark_load_complete()

        plan_obj = None
        if config.plan_path:
            plan_obj = _read_plan(config.plan_path)
            if plan_obj and not config.quiet:
                info(
                    logger,
                    "plan_loaded",
                    plan_file=config.plan_path,
                    action_count=len(plan_obj.get("actions", [])),
                )

        # Create graph builder and generate DOT
        builder = GraphBuilder(config, metrics, logger)
        dot_content = builder.build_dot(contract, plan_obj)

        # Write output
        _write_output(dot_content, config, metrics, logger)
        metrics.mark_render_complete()

        # Log completion with metrics
        if not config.quiet:
            metrics_data = metrics.to_dict()
            info(
                logger,
                "viz_graph_complete",
                output_file=config.output_path,
                format=config.format,
                **metrics_data,
            )

        # Debug output
        if getattr(args, "debug", False):
            debug_dir = Path(config.output_path).parent / "debug"
            debug_dir.mkdir(exist_ok=True)

            # Save DOT file
            dot_file = debug_dir / f"{Path(config.output_path).stem}.dot"
            dot_file.write_text(dot_content, encoding="utf-8")

            # Save metrics
            metrics_file = debug_dir / f"{Path(config.output_path).stem}_metrics.json"
            metrics_file.write_text(json.dumps(metrics.to_dict(), indent=2), encoding="utf-8")

            info(
                logger, "debug_files_saved", dot_file=str(dot_file), metrics_file=str(metrics_file)
            )

        return 0

    except CLIError:
        # Re-raise known CLI errors
        raise
    except FileNotFoundError as e:
        error(logger, "file_not_found", error=str(e))
        raise CLIError(2, "file_not_found", {"error": str(e)})
    except ValueError as e:
        error(logger, "validation_error", error=str(e))
        raise CLIError(2, "validation_error", {"error": str(e)})
    except Exception as e:
        error(logger, "unexpected_error", error=str(e), type=type(e).__name__)
        if getattr(args, "debug", False):
            import traceback

            error(logger, "debug_traceback", traceback=traceback.format_exc())
        raise CLIError(1, "unexpected_error", {"error": str(e)})


def _run_provider_actions_viz(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Handle provider actions visualization for FLUID 0.7.1.

    This function is called when --provider-actions or --actions-only flags are used.
    It extracts provider actions from the contract and generates a dependency graph.
    """
    try:
        from .viz_provider_actions import (
            add_provider_actions_to_viz,
            visualize_provider_actions_dot,
            visualize_provider_actions_html,
        )
    except ImportError as e:
        logger.error(f"Provider actions visualization not available: {e}")
        logger.info(
            "This is a FLUID 0.7.1 feature. Ensure viz_provider_actions module is available."
        )
        return 1

    # Load contract
    contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)

    # Extract provider actions
    result = add_provider_actions_to_viz(contract, logger)

    if result is None:
        logger.warning("No provider actions found in contract")
        logger.info(
            "Hint: This is a FLUID 0.7.1 feature. Ensure your contract has a 'providerActions' section."
        )
        return 1

    actions, dependencies = result
    logger.info(
        f"Found {len(actions)} provider actions with {sum(len(d) for d in dependencies.values())} dependencies"
    )

    # Get output configuration
    output_format = getattr(args, "format", "svg")
    output_path = getattr(args, "output_path", "runtime/graph/provider_actions.svg")

    # Ensure output directory exists
    out_dir = Path(output_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate visualization based on format
    if output_format == "html":
        html_content = visualize_provider_actions_html(actions, dependencies)
        html_path = (
            str(output_path)
            .replace(".svg", ".html")
            .replace(".png", ".html")
            .replace(".dot", ".html")
        )

        with open(html_path, "w") as f:
            f.write(html_content)

        logger.info(f"✅ Provider actions visualization saved to: {html_path}")

        if getattr(args, "open_when_done", False):
            _shell_open(Path(html_path), logger)

        return 0

    else:
        # Generate DOT format
        dot_content = visualize_provider_actions_dot(actions, dependencies)

        if output_format == "dot":
            dot_path = (
                str(output_path)
                .replace(".svg", ".dot")
                .replace(".png", ".dot")
                .replace(".html", ".dot")
            )

            with open(dot_path, "w") as f:
                f.write(dot_content)

            logger.info(f"✅ Provider actions DOT saved to: {dot_path}")
            logger.info(
                "You can paste the contents into https://dreampuf.github.io/GraphvizOnline/ to visualize"
            )
            return 0

        else:
            # Render with Graphviz (SVG or PNG)
            graphviz_available, graphviz_version = _check_graphviz_installation()

            if not graphviz_available:
                logger.error(
                    "Graphviz not installed. Install with: sudo apt-get install graphviz (Linux) or brew install graphviz (macOS)"
                )
                logger.info("Falling back to DOT format...")
                dot_path = str(output_path).replace(".svg", ".dot").replace(".png", ".dot")
                with open(dot_path, "w") as f:
                    f.write(dot_content)
                logger.info(f"DOT file saved to: {dot_path}")
                return 1

            # Write DOT to temp file
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", suffix=".dot", delete=False) as tmp_dot:
                tmp_dot.write(dot_content)
                tmp_dot_path = tmp_dot.name

            try:
                # Render with Graphviz
                subprocess.run(
                    ["dot", f"-T{output_format}", tmp_dot_path, "-o", output_path],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )

                logger.info(f"✅ Provider actions visualization saved to: {output_path}")

                if getattr(args, "open_when_done", False):
                    _shell_open(Path(output_path), logger)

                return 0

            except subprocess.CalledProcessError as e:
                logger.error(
                    f"Graphviz rendering failed: {e.stderr.decode() if e.stderr else str(e)}"
                )
                return 1
            except subprocess.TimeoutExpired:
                logger.error("Graphviz rendering timed out")
                return 1
            finally:
                # Clean up temp file
                import os

                try:
                    os.unlink(tmp_dot_path)
                except OSError:
                    pass
