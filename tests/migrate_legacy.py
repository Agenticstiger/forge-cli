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
FLUID Build Test Migration Script

This script helps migrate from the old scattered test scripts to the new consolidated test structure.
It identifies old test scripts and provides options to archive or remove them.
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

# Rich imports for enhanced output (optional)
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm
    from rich.table import Table

    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None


def find_project_root() -> Path:
    """Find the FLUID Build project root"""
    current = Path.cwd()

    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists() and (parent / "fluid_build").exists():
            return parent

    return current


def find_legacy_test_scripts(project_root: Path) -> list:
    """Find old test scripts that should be migrated"""
    legacy_scripts = []

    # Check scripts directory
    scripts_dir = project_root / "scripts"
    if scripts_dir.exists():
        patterns = ["*test*.py", "*Test*.py", "run_all_tests.*", "ultimate_test_suite.*"]

        for pattern in patterns:
            for script in scripts_dir.glob(pattern):
                if script.is_file():
                    legacy_scripts.append(script)

    # Check root directory for scattered test files
    root_patterns = ["test_*.py", "*_test.py", "run_tests.py"]

    for pattern in root_patterns:
        for script in project_root.glob(pattern):
            if script.is_file() and "tests/" not in str(script):
                legacy_scripts.append(script)

    return list(set(legacy_scripts))  # Remove duplicates


def archive_legacy_scripts(project_root: Path, legacy_scripts: list) -> Path:
    """Archive legacy test scripts to a backup directory"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = project_root / "legacy_tests_backup" / f"archived_{timestamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived = []
    for script in legacy_scripts:
        try:
            # Create relative path structure in archive
            rel_path = script.relative_to(project_root)
            archive_path = archive_dir / rel_path
            archive_path.parent.mkdir(parents=True, exist_ok=True)

            # Copy to archive
            shutil.copy2(script, archive_path)
            archived.append((script, archive_path))

        except Exception as e:
            if RICH_AVAILABLE and console:
                console.print(f"[red]❌ Failed to archive {script}: {e}[/red]")
            else:
                print(f"❌ Failed to archive {script}: {e}")

    return archive_dir


def main():
    """Main migration function"""
    if RICH_AVAILABLE and console:
        console.print(
            Panel(
                "[bold cyan]🔄 FLUID Build Test Migration Tool[/bold cyan]\n\n"
                "This tool helps migrate from old scattered test scripts\n"
                "to the new consolidated test structure.",
                title="📦 Migration Assistant",
                style="blue",
            )
        )
    else:
        print("🔄 FLUID Build Test Migration Tool")
        print("=" * 50)
        print("This tool helps migrate from old scattered test scripts")
        print("to the new consolidated test structure.")
        print("=" * 50)

    project_root = find_project_root()

    # Check if new test structure exists
    new_tests_dir = project_root / "tests"
    if not new_tests_dir.exists():
        if RICH_AVAILABLE and console:
            console.print(
                "[red]❌ New test structure not found. Please set up the consolidated tests first.[/red]"
            )
        else:
            print("❌ New test structure not found. Please set up the consolidated tests first.")
        return 1

    # Find legacy scripts
    legacy_scripts = find_legacy_test_scripts(project_root)

    if not legacy_scripts:
        if RICH_AVAILABLE and console:
            console.print(
                "[green]✅ No legacy test scripts found. Migration already complete![/green]"
            )
        else:
            print("✅ No legacy test scripts found. Migration already complete!")
        return 0

    # Display found scripts
    if RICH_AVAILABLE and console:
        console.print(
            f"\n[bold yellow]📋 Found {len(legacy_scripts)} legacy test script(s):[/bold yellow]"
        )

        table = Table()
        table.add_column("Script", style="cyan")
        table.add_column("Location", style="dim")
        table.add_column("Size", style="green")

        for script in legacy_scripts:
            rel_path = script.relative_to(project_root)
            size = f"{script.stat().st_size:,} bytes"
            table.add_row(script.name, str(rel_path.parent), size)

        console.print(table)
    else:
        print(f"\n📋 Found {len(legacy_scripts)} legacy test script(s):")
        for script in legacy_scripts:
            rel_path = script.relative_to(project_root)
            size = script.stat().st_size
            print(f"  • {script.name} ({rel_path.parent}) - {size:,} bytes")

    # Ask user what to do
    if RICH_AVAILABLE and console:
        console.print(
            "\n[bold yellow]🤔 What would you like to do with these legacy scripts?[/bold yellow]"
        )
        console.print("1. [green]Archive[/green] - Move to backup directory (recommended)")
        console.print("2. [red]Remove[/red] - Delete permanently")
        console.print("3. [blue]Keep[/blue] - Leave as-is (not recommended)")

        choice = console.input("\n[bold]Enter your choice (1-3): [/bold]")
    else:
        print("\n🤔 What would you like to do with these legacy scripts?")
        print("1. Archive - Move to backup directory (recommended)")
        print("2. Remove - Delete permanently")
        print("3. Keep - Leave as-is (not recommended)")
        choice = input("\nEnter your choice (1-3): ")

    if choice == "1":
        # Archive scripts
        archive_dir = archive_legacy_scripts(project_root, legacy_scripts)

        if RICH_AVAILABLE and console:
            console.print("\n[green]✅ Archived legacy scripts to:[/green]")
            console.print(f"   {archive_dir}")

            if Confirm.ask("\n[yellow]Remove original files after archiving?[/yellow]"):
                for script in legacy_scripts:
                    script.unlink()
                console.print("[green]✅ Original files removed[/green]")
        else:
            print(f"\n✅ Archived legacy scripts to: {archive_dir}")

            response = input("\nRemove original files after archiving? (y/N): ")
            if response.lower() in ["y", "yes"]:
                for script in legacy_scripts:
                    script.unlink()
                print("✅ Original files removed")

    elif choice == "2":
        # Remove scripts
        if RICH_AVAILABLE and console:
            if Confirm.ask(
                "[red]⚠️ Are you sure you want to permanently delete these scripts?[/red]"
            ):
                for script in legacy_scripts:
                    script.unlink()
                console.print("[green]✅ Legacy scripts removed[/green]")
        else:
            response = input("⚠️ Are you sure you want to permanently delete these scripts? (y/N): ")
            if response.lower() in ["y", "yes"]:
                for script in legacy_scripts:
                    script.unlink()
                print("✅ Legacy scripts removed")

    elif choice == "3":
        # Keep scripts
        if RICH_AVAILABLE and console:
            console.print(
                "[yellow]⚠️ Legacy scripts will be kept. This may cause confusion.[/yellow]"
            )
            console.print("[dim]Consider archiving them later for cleaner project structure.[/dim]")
        else:
            print("⚠️ Legacy scripts will be kept. This may cause confusion.")
            print("Consider archiving them later for cleaner project structure.")

    # Show migration status
    if RICH_AVAILABLE and console:
        console.print("\n[bold green]✅ Migration Assessment:[/bold green]")
        console.print("   • New consolidated test structure: [green]✅ Available[/green]")
        console.print(
            f"   • Legacy scripts: [{'green' if choice in ['1', '2'] else 'yellow'}]{'✅ Handled' if choice in ['1', '2'] else '⚠️ Still present'}[/{'green' if choice in ['1', '2'] else 'yellow'}]"
        )
        console.print("   • Admin command integration: [green]✅ Ready[/green]")

        console.print("\n[bold cyan]🎯 Next Steps:[/bold cyan]")
        console.print("   1. Use [bold]fluid admin test[/bold] for integrated testing")
        console.print("   2. Use [bold]python tests/run_tests.py[/bold] for direct execution")
        console.print("   3. Check reports in [bold]runtime/test_runs/[/bold]")
    else:
        print("\n✅ Migration Assessment:")
        print("   • New consolidated test structure: ✅ Available")
        print(f"   • Legacy scripts: {'✅ Handled' if choice in ['1', '2'] else '⚠️ Still present'}")
        print("   • Admin command integration: ✅ Ready")

        print("\n🎯 Next Steps:")
        print("   1. Use 'fluid admin test' for integrated testing")
        print("   2. Use 'python tests/run_tests.py' for direct execution")
        print("   3. Check reports in 'runtime/test_runs/'")

    return 0


if __name__ == "__main__":
    sys.exit(main())
