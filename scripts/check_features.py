#!/usr/bin/env python3
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
Show feature status and what's included in each build profile.

This is a simple status viewer for the MVP feature release system.
Run with: python scripts/check_features.py
"""

import sys
from pathlib import Path

# Add parent to path so we can import fluid_build
sys.path.insert(0, str(Path(__file__).parent.parent))


def show_status():
    """Display feature status in a nice table format."""
    try:
        from rich import box
        from rich.console import Console
        from rich.table import Table

        HAS_RICH = True
    except ImportError:
        HAS_RICH = False
        print("Tip: Install 'rich' for better output: pip install rich")
        print()

    import yaml

    # Load manifest
    manifest_path = Path(__file__).parent.parent / "fluid_build" / "features.yaml"
    if not manifest_path.exists():
        print(f"Error: {manifest_path} not found")
        return 1

    with open(manifest_path) as f:
        features = yaml.safe_load(f)

    # Get metadata (new structure) or fallback to root level (old structure)
    metadata = features.get("metadata", {})
    current_release = metadata.get("current_release", features.get("current_release", "N/A"))
    last_updated = metadata.get("last_updated", features.get("last_updated", "N/A"))

    if HAS_RICH:
        console = Console()

        # Header
        console.print("\n[bold cyan]FLUID Build Feature Status[/bold cyan]")
        console.print(f"Version: {current_release} | Updated: {last_updated}\n")

        # Providers table
        providers_table = Table(title="Providers", box=box.ROUNDED, show_header=True)
        providers_table.add_column("Provider", style="cyan bold", width=12)
        providers_table.add_column("Status", width=10)
        providers_table.add_column("Coverage", width=10, justify="right")
        providers_table.add_column("Docs", width=8)
        providers_table.add_column("Note", style="dim")

        status_colors = {"stable": "green", "beta": "yellow", "alpha": "red"}

        for name, config in sorted(features["providers"].items()):
            status = config["status"]
            color = status_colors.get(status, "white")
            coverage = f"{config.get('test_coverage', 0)}%"
            docs = "✓" if config.get("docs_complete") else "✗"

            providers_table.add_row(
                name,
                f"[{color} bold]{status}[/{color} bold]",
                coverage,
                f"[{'green' if config.get('docs_complete') else 'red'}]{docs}[/{'green' if config.get('docs_complete') else 'red'}]",
                config.get("reason", ""),
            )

        console.print(providers_table)
        console.print()

        # Command groups table
        commands_table = Table(title="Command Groups", box=box.ROUNDED, show_header=True)
        commands_table.add_column("Group", style="cyan bold", width=15)
        commands_table.add_column("Status", width=10)
        commands_table.add_column("Commands", width=8, justify="right")
        commands_table.add_column("Description", style="dim")

        for name, config in sorted(features["command_groups"].items()):
            status = config["status"]
            color = status_colors.get(status, "white")
            cmd_count = len(config.get("commands", []))

            commands_table.add_row(
                name,
                f"[{color} bold]{status}[/{color} bold]",
                str(cmd_count),
                config.get("description", ""),
            )

        console.print(commands_table)
        console.print()

        # Build profiles
        console.print("[bold cyan]Build Profiles:[/bold cyan]\n")
        for profile, config in features["build_profiles"].items():
            status_list = ", ".join(config["include_status"])
            console.print(f"  [yellow bold]{profile:12}[/yellow bold] {config['description']}")
            console.print(f"  {'':12} Includes: [dim]{status_list}[/dim]")
            console.print(f"  {'':12} Audience: [dim]{config['target_audience']}[/dim]")
            console.print()

        # Summary
        total_providers = len(features["providers"])
        stable_providers = sum(1 for p in features["providers"].values() if p["status"] == "stable")
        total_groups = len(features["command_groups"])
        stable_groups = sum(
            1 for g in features["command_groups"].values() if g["status"] == "stable"
        )

        console.print("[bold]Summary:[/bold]")
        console.print(f"  Providers: {stable_providers}/{total_providers} stable")
        console.print(f"  Command Groups: {stable_groups}/{total_groups} stable")
        console.print()

    else:
        # Fallback to plain text
        print("FLUID Build Feature Status")
        print(f"Version: {current_release}")
        print(f"Updated: {last_updated}")
        print()

        print("PROVIDERS:")
        print("-" * 80)
        for name, config in sorted(features["providers"].items()):
            status = config["status"].upper()
            coverage = config.get("test_coverage", 0)
            docs = "✓" if config.get("docs_complete") else "✗"
            print(f"  {name:12} {status:8} Coverage: {coverage:3}%  Docs: {docs}")
            print(f"  {'':12} {config.get('reason', '')}")
            print()

        print("\nCOMMAND GROUPS:")
        print("-" * 80)
        for name, config in sorted(features["command_groups"].items()):
            status = config["status"].upper()
            cmd_count = len(config.get("commands", []))
            print(f"  {name:15} {status:8} Commands: {cmd_count}")
            print(f"  {'':15} {config.get('description', '')}")
            print()

        print("\nBUILD PROFILES:")
        print("-" * 80)
        for profile, config in features["build_profiles"].items():
            status_list = ", ".join(config["include_status"])
            print(f"  {profile:12} {config['description']}")
            print(f"  {'':12} Includes: {status_list}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(show_status())
