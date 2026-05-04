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

"""HTML wrapper — wraps a generated SVG in an interactive HTML shell with theme + zoom + tooltip support. Extracted from ``viz_graph.py`` so the static HTML / CSS / JS template lives in its own file.

The public entry point :func:`_create_html_wrapper` is re-imported by
``viz_graph`` at top level so existing test patches that target
``fluid_build.cli.viz_graph._create_html_wrapper`` still resolve.
"""

from __future__ import annotations

import html as html_module
import logging
import re
import subprocess
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from fluid_build.cli.viz_graph import (
    THEMES,
    GraphConfig,
    GraphMetrics,
    _escape_label,
    _get_theme_value,
    _safe_id,
)


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
