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
FLUID Version Command - Unified Implementation

Shows FLUID CLI version, supported specifications, and feature availability.
Automatically detects available features and provides system diagnostics.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
from typing import Any, Dict

from fluid_build.cli.console import cprint

from .. import __version__ as CLI_VERSION
from ..schema_manager import FluidSchemaManager
from ._common import CLIError
from ._logging import info

# Try Rich for better output
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

COMMAND = "version"
FALLBACK_SPEC_VERSION = "0.5.7"


def register(subparsers: argparse._SubParsersAction):
    """Register unified version command"""
    supported_versions = ", ".join(FluidSchemaManager.BUNDLED_VERSIONS)
    p = subparsers.add_parser(
        COMMAND,
        help="Show version and system information",
        description=f"""
Display FLUID CLI version and system information.

Shows:
• CLI version and build info
• Supported FLUID spec versions ({supported_versions})
• Available features and capabilities
• Provider availability
• System/Python environment (with --verbose)
        """.strip(),
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Show detailed system information")
    p.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    p.add_argument("--short", action="store_true", help="Show only version number")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Main entry point with feature detection"""
    try:
        # Short mode - just version number
        if getattr(args, "short", False):
            cprint(CLI_VERSION)
            return 0

        # Gather version information
        version_info = _gather_version_info(args)

        # Output in requested format
        output_format = getattr(args, "format", "text")
        if output_format == "json":
            cprint(json.dumps(version_info, indent=2))
        else:
            _display_version_info(version_info, getattr(args, "verbose", False))

        info(logger, "version", **{"cli_version": version_info["cli"]["version"]})
        return 0

    except Exception as e:
        raise CLIError(1, "version_failed", context={"error": str(e)})


def _gather_version_info(args) -> Dict[str, Any]:
    """Gather comprehensive version information"""
    supported_versions = FluidSchemaManager.BUNDLED_VERSIONS
    latest_version = supported_versions[-1] if supported_versions else FALLBACK_SPEC_VERSION
    default_version = latest_version
    version_info = {
        "cli": {"version": CLI_VERSION, "api_version": "v1", "build": "production"},
        "spec_versions": {
            "supported": supported_versions,
            "default": default_version,
            "latest": latest_version,
        },
        "features": _detect_features(),
        "providers": _detect_providers(),
    }

    # Add verbose information
    if getattr(args, "verbose", False):
        version_info["python"] = {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "platform": sys.platform,
        }
        version_info["system"] = {
            "platform": platform.platform(),
            "system": platform.system(),
            "machine": platform.machine(),
        }

    return version_info


def _detect_features() -> Dict[str, bool]:
    """Detect which FLUID features are available"""
    features = {
        "core_validation": True,  # Always available
        "legacy_057": True,  # Always available
    }

    # Check 0.7.1+ features
    try:
        from fluid_build.forge.core.provider_actions import ProviderActionParser

        features["provider_actions"] = True
        features["0.7.1_support"] = True
    except ImportError:
        features["provider_actions"] = False
        features["0.7.1_support"] = False

    try:
        from fluid_build.policy.sovereignty import SovereigntyValidator

        features["sovereignty"] = True
    except ImportError:
        features["sovereignty"] = False

    try:
        from fluid_build.policy.agent_policy import AgentPolicyValidator

        features["agent_policy"] = True
    except ImportError:
        features["agent_policy"] = False

    try:
        from fluid_build.runtimes.airflow_provider_actions import AirflowDAGGenerator

        features["airflow_generation"] = True
    except ImportError:
        features["airflow_generation"] = False

    return features


def _detect_providers() -> Dict[str, str]:
    """Detect available providers"""
    providers = {}

    try:
        # Simplified - just check if module loads
        providers["local"] = "available"

        try:
            from fluid_build.providers.gcp import GCPProvider

            providers["gcp"] = "available"
        except ImportError:
            providers["gcp"] = "not installed"

        try:
            from fluid_build.providers.aws import AWSProvider

            providers["aws"] = "available"
        except ImportError:
            providers["aws"] = "not installed"

        try:
            from fluid_build.providers.snowflake import SnowflakeProvider

            providers["snowflake"] = "available"
        except ImportError:
            providers["snowflake"] = "not installed"
    except Exception as e:
        providers["error"] = str(e)

    return providers


def _display_version_info(version_info: Dict[str, Any], verbose: bool = False):
    """Display version information with appropriate formatting"""

    if RICH_AVAILABLE:
        console = Console()

        # Main version panel
        cli_info = version_info["cli"]
        spec_info = version_info["spec_versions"]

        version_text = f"""[bold cyan]FLUID CLI[/bold cyan]
Version: {cli_info["version"]}
API: {cli_info["api_version"]}

[bold cyan]Supported Specifications:[/bold cyan]
• FLUID {', '.join(spec_info['supported'])}
• Default: {spec_info['default']}
• Latest: {spec_info['latest']}"""

        console.print(Panel(version_text, title="📦 Version Information", border_style="cyan"))

        # Features table
        features = version_info["features"]
        table = Table(title="✨ Available Features", show_header=True)
        table.add_column("Feature", style="cyan")
        table.add_column("Status", width=15)

        feature_names = {
            "core_validation": "Core Validation",
            "legacy_057": "FLUID 0.5.7 Support",
            "0.7.1_support": "FLUID 0.7.1+ Support",
            "provider_actions": "Provider Actions",
            "sovereignty": "Sovereignty Constraints",
            "agent_policy": "Agent Policy",
            "airflow_generation": "Airflow DAG Generation",
        }

        for key, name in feature_names.items():
            if key in features:
                status = (
                    "[green]✅ Available[/green]"
                    if features[key]
                    else "[yellow]⚠️  Not available[/yellow]"
                )
                table.add_row(name, status)

        console.print(table)

        # Providers (if available)
        if version_info.get("providers"):
            cprint()
            providers_text = "\n".join(
                [
                    f"• {name}: {status}"
                    for name, status in version_info["providers"].items()
                    if name != "error"
                ]
            )
            console.print(Panel(providers_text, title="🔌 Providers", border_style="blue"))

        # Verbose system info
        if verbose and "python" in version_info:
            cprint()
            py_info = version_info["python"]
            sys_info = version_info["system"]
            console.print(f"[dim]Python: {py_info['version']} ({py_info['platform']})[/dim]")
            console.print(f"[dim]System: {sys_info['system']} {sys_info['machine']}[/dim]")

    else:
        # Simple text output
        cli_info = version_info["cli"]
        spec_info = version_info["spec_versions"]

        cprint("\n" + "=" * 60)
        cprint("FLUID CLI")
        cprint("=" * 60)
        cprint(f"Version: {cli_info['version']}")
        cprint(f"API: {cli_info['api_version']}")
        cprint(f"\nSupported Specifications: {', '.join(spec_info['supported'])}")
        cprint(f"Default: {spec_info['default']}")
        cprint(f"Latest: {spec_info['latest']}")

        cprint("\n" + "=" * 60)
        cprint("Available Features")
        cprint("=" * 60)

        for key, available in version_info["features"].items():
            status = "✅" if available else "⚠️ "
            cprint(f"{status} {key.replace('_', ' ').title()}")

        if version_info.get("providers"):
            cprint("\n" + "=" * 60)
            cprint("Providers")
            cprint("=" * 60)
            for name, status in version_info["providers"].items():
                if name != "error":
                    cprint(f"• {name}: {status}")

        cprint("=" * 60 + "\n")
