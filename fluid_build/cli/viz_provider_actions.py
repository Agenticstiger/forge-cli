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

"""
Visualization support for FLUID 0.7.1 provider actions.
Generates dependency graphs for provider action execution plans.
"""

from typing import Dict, List, Optional, Tuple


def visualize_provider_actions_dot(actions: List[Dict], dependencies: Dict[str, List[str]]) -> str:
    """
    Generate DOT format visualization of provider actions.

    Args:
        actions: List of provider action dictionaries
        dependencies: Dependency graph {action_id: [depends_on_ids]}

    Returns:
        DOT format string
    """
    lines = []
    lines.append("digraph ProviderActions {")
    lines.append("  rankdir=TB;")
    lines.append('  node [shape=box, style="rounded,filled", fontname="Helvetica"];')
    lines.append('  edge [fontname="Helvetica", fontsize=10];')
    lines.append("")

    # Group actions by provider
    provider_actions = {}
    for action in actions:
        provider = action.get("provider", "unknown")
        if provider not in provider_actions:
            provider_actions[provider] = []
        provider_actions[provider].append(action)

    # Define colors for different providers
    provider_colors = {
        "gcp": "#4285F4",  # Google Blue
        "aws": "#FF9900",  # AWS Orange
        "azure": "#0078D4",  # Azure Blue
        "snowflake": "#29B5E8",  # Snowflake Blue
        "databricks": "#FF3621",  # Databricks Red
        "airflow": "#017CEE",  # Airflow Blue
    }

    # Define colors for different action types
    action_type_colors = {
        "create": "#34A853",  # Green
        "update": "#FBBC04",  # Yellow
        "delete": "#EA4335",  # Red
        "query": "#4285F4",  # Blue
        "transform": "#9334E6",  # Purple
        "schedule": "#F538A0",  # Pink
        "monitor": "#00ACC1",  # Cyan
        "notify": "#F4B400",  # Amber
        "configure": "#5E35B1",  # Deep Purple
    }

    # Create subgraph for each provider
    for provider, provider_action_list in provider_actions.items():
        provider_color = provider_colors.get(provider, "#607D8B")

        lines.append(f"  subgraph cluster_{provider} {{")
        lines.append(f'    label="{provider.upper()}";')
        lines.append("    style=filled;")
        lines.append(f'    color="{provider_color}20";')
        lines.append("    fontsize=14;")
        lines.append(f'    fontcolor="{provider_color}";')
        lines.append("")

        for action in provider_action_list:
            action_id = action.get("action_id", action.get("id", "unknown"))
            action_type = action.get("action_type", action.get("type", "unknown"))
            resource = action.get("resource", action.get("params", {}).get("table", "unknown"))

            # Get color based on action type
            node_color = action_type_colors.get(action_type.lower(), provider_color)

            # Create label with type and resource
            label = f"{action_type}\\n{resource}"

            # Add execution info if available
            if "execution_time" in action:
                label += f"\\n({action['execution_time']}s)"

            lines.append(f'    "{action_id}" [')
            lines.append(f'      label="{label}",')
            lines.append(f'      fillcolor="{node_color}",')
            lines.append('      fontcolor="white"')
            lines.append("    ];")

        lines.append("  }")
        lines.append("")

    # Add dependency edges
    lines.append("  // Dependencies")
    for action_id, depends_on_list in dependencies.items():
        for depends_on in depends_on_list:
            lines.append(f'  "{depends_on}" -> "{action_id}" [')
            lines.append('    label="depends",')
            lines.append('    color="#999999",')
            lines.append('    fontcolor="#666666"')
            lines.append("  ];")

    # Add execution order if available
    {a.get("action_id", a.get("id")): a for a in actions}
    for i, action in enumerate(actions):
        action_id = action.get("action_id", action.get("id"))
        if i < len(actions) - 1:
            next_action = actions[i + 1]
            next_id = next_action.get("action_id", next_action.get("id"))

            # Only add order edge if not already a dependency edge
            if next_id not in dependencies.get(action_id, []):
                lines.append(f'  "{action_id}" -> "{next_id}" [')
                lines.append("    style=dotted,")
                lines.append('    color="#CCCCCC",')
                lines.append('    label="then",')
                lines.append("    fontsize=9,")
                lines.append('    fontcolor="#999999"')
                lines.append("  ];")

    lines.append("}")

    return "\n".join(lines)


def visualize_provider_actions_html(actions: List[Dict], dependencies: Dict[str, List[str]]) -> str:
    """
    Generate interactive HTML visualization of provider actions.

    Args:
        actions: List of provider action dictionaries
        dependencies: Dependency graph

    Returns:
        HTML string with embedded SVG or canvas visualization
    """
    # First generate DOT
    dot_content = visualize_provider_actions_dot(actions, dependencies)

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>FLUID Provider Actions Visualization</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background: #1e1e1e;
            color: #d4d4d4;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        h1 {{
            color: #4FC3F7;
            border-bottom: 2px solid #4FC3F7;
            padding-bottom: 10px;
        }}
        .stats {{
            background: #252526;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
        }}
        .stat-item {{
            text-align: center;
        }}
        .stat-value {{
            font-size: 32px;
            font-weight: bold;
            color: #4FC3F7;
        }}
        .stat-label {{
            font-size: 14px;
            color: #858585;
            margin-top: 5px;
        }}
        .graph-container {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            overflow: auto;
        }}
        .action-list {{
            background: #252526;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
        }}
        .action-item {{
            background: #2d2d30;
            padding: 15px;
            margin: 10px 0;
            border-left: 4px solid #4FC3F7;
            border-radius: 4px;
        }}
        .action-id {{
            font-weight: bold;
            color: #4FC3F7;
        }}
        .action-type {{
            color: #4EC9B0;
            font-size: 14px;
        }}
        .action-provider {{
            color: #CE9178;
            font-size: 12px;
        }}
        .dependencies {{
            margin-top: 10px;
            font-size: 12px;
            color: #858585;
        }}
        pre {{
            background: #1e1e1e;
            padding: 10px;
            border-radius: 4px;
            overflow-x: auto;
            font-size: 11px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 FLUID 0.7.1 Provider Actions</h1>
        
        <div class="stats">
            <div class="stat-item">
                <div class="stat-value">{len(actions)}</div>
                <div class="stat-label">Total Actions</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{len(set(a.get('provider', 'unknown') for a in actions))}</div>
                <div class="stat-label">Providers</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{sum(len(deps) for deps in dependencies.values())}</div>
                <div class="stat-label">Dependencies</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{len(set(a.get('action_type', 'unknown') for a in actions))}</div>
                <div class="stat-label">Action Types</div>
            </div>
        </div>
        
        <div class="action-list">
            <h2>Actions (Execution Order)</h2>
            {''.join(_render_action_item(a, dependencies) for a in actions)}
        </div>
        
        <div class="graph-container">
            <h2>Dependency Graph (DOT Format)</h2>
            <p>Paste this into <a href="https://dreampuf.github.io/GraphvizOnline/" target="_blank">GraphvizOnline</a> to visualize:</p>
            <pre>{dot_content}</pre>
        </div>
    </div>
</body>
</html>
"""

    return html


def _render_action_item(action: Dict, dependencies: Dict[str, List[str]]) -> str:
    """Render a single action item for HTML output."""
    action_id = action.get("action_id", action.get("id", "unknown"))
    action_type = action.get("action_type", action.get("type", "unknown"))
    provider = action.get("provider", "unknown")
    depends_on = dependencies.get(action_id, [])

    deps_html = ""
    if depends_on:
        deps_html = f'<div class="dependencies">Depends on: {", ".join(depends_on)}</div>'

    return f"""
        <div class="action-item">
            <div class="action-id">{action_id}</div>
            <div class="action-type">{action_type}</div>
            <div class="action-provider">Provider: {provider}</div>
            {deps_html}
        </div>
    """


def add_provider_actions_to_viz(
    contract: Dict, logger
) -> Optional[Tuple[List[Dict], Dict[str, List[str]]]]:
    """
    Extract provider actions from contract for visualization.

    Args:
        contract: FLUID contract dictionary
        logger: Logger instance

    Returns:
        (actions, dependencies) tuple or None if no provider actions
    """
    try:
        from ..forge.core.provider_actions import ProviderActionParser

        parser = ProviderActionParser(logger)
        actions_list = parser.parse(contract)

        if not actions_list:
            return None

        # Build dependency graph
        dependencies = {}
        for action in actions_list:
            if action.depends_on:
                dependencies[action.action_id] = action.depends_on

        # Convert to dictionaries for visualization
        actions_dicts = [
            {
                "action_id": a.action_id,
                "action_type": a.action_type.value,
                "provider": a.provider,
                "resource": a.params.get("table")
                or a.params.get("bucket")
                or a.params.get("dataset"),
                "params": a.params,
            }
            for a in actions_list
        ]

        return actions_dicts, dependencies

    except ImportError:
        logger.debug("Provider action parser not available")
        return None
    except Exception as e:
        logger.error(f"Error parsing provider actions: {e}")
        return None
