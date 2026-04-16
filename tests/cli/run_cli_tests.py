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
FLUID Build - CLI Test Runner

Unified test runner for all CLI command tests.
Executes all test files and provides comprehensive reporting.

Usage:
    python run_cli_tests.py              # Run all tests
    python run_cli_tests.py validate     # Run tests for validate command only
    python run_cli_tests.py --verbose    # Run with verbose output
    python run_cli_tests.py --list       # List all available test modules
"""

import argparse
import sys
import time
import unittest
from pathlib import Path
from typing import List

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Try to import rich for beautiful output
try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    HAS_RICH = True
    console = Console()
except ImportError:
    HAS_RICH = False
    console = None


# All CLI command test modules
TEST_MODULES = [
    "test_validate",
    "test_plan",
    "test_apply",
    "test_version_cmd",
    "test_version_enhanced",
    "test_provider_cmds",
    "test_doctor",
    "test_docs_build",
    "test_contract_tests",
    "test_viz_plan",
    "test_viz_graph",
    "test_viz_graph_enhanced",
    "test_scaffold_ci",
    "test_scaffold_composer",
    "test_export_opds",
    "test_export_opds_enhanced",
    "test_opds",
    "test_admin",
    "test_forge",
    "test_market",
    "test_auth",
]


class TestResult:
    """Container for test results."""

    def __init__(
        self, module: str, passed: int, failed: int, errors: int, skipped: int, time: float
    ):
        self.module = module
        self.passed = passed
        self.failed = failed
        self.errors = errors
        self.skipped = skipped
        self.time = time
        self.total = passed + failed + errors + skipped

    @property
    def success(self) -> bool:
        """Check if all tests passed."""
        return self.failed == 0 and self.errors == 0

    def __repr__(self):
        return f"TestResult({self.module}: {self.passed}/{self.total} passed)"


def run_test_module(module_name: str, verbosity: int = 1) -> TestResult:
    """Run tests for a single module."""
    try:
        # Import the test module
        test_module = __import__(f"tests.cli.{module_name}", fromlist=[""])

        # Load tests from the module
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(test_module)

        # Run tests
        runner = unittest.TextTestRunner(
            verbosity=verbosity, stream=sys.stdout if verbosity > 1 else None
        )
        start_time = time.time()
        result = runner.run(suite)
        elapsed_time = time.time() - start_time

        # Extract results
        total_run = result.testsRun
        failed = len(result.failures)
        errors = len(result.errors)
        skipped = len(result.skipped)
        passed = total_run - failed - errors - skipped

        return TestResult(module_name, passed, failed, errors, skipped, elapsed_time)

    except ImportError as e:
        print(f"Warning: Could not import {module_name}: {e}")
        return TestResult(module_name, 0, 0, 1, 0, 0.0)
    except Exception as e:
        print(f"Error running {module_name}: {e}")
        return TestResult(module_name, 0, 0, 1, 0, 0.0)


def print_results_rich(results: List[TestResult], total_time: float):
    """Print test results using rich formatting."""
    # Summary table
    table = Table(title="CLI Test Results", box=box.ROUNDED, show_header=True)
    table.add_column("Module", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Tests", justify="right")
    table.add_column("Passed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Errors", justify="right", style="red")
    table.add_column("Time", justify="right")

    total_tests = 0
    total_passed = 0
    total_failed = 0
    total_errors = 0

    for result in results:
        status = "✅ PASS" if result.success else "❌ FAIL"
        status_style = "green" if result.success else "red"

        table.add_row(
            result.module.replace("test_", ""),
            f"[{status_style}]{status}[/{status_style}]",
            str(result.total),
            str(result.passed),
            str(result.failed),
            str(result.errors),
            f"{result.time:.2f}s",
        )

        total_tests += result.total
        total_passed += result.passed
        total_failed += result.failed
        total_errors += result.errors

    # Add summary row
    table.add_section()
    overall_status = "✅ ALL PASS" if all(r.success for r in results) else "❌ SOME FAILED"
    overall_style = "green bold" if all(r.success for r in results) else "red bold"

    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[{overall_style}]{overall_status}[/{overall_style}]",
        f"[bold]{total_tests}[/bold]",
        f"[bold green]{total_passed}[/bold green]",
        f"[bold red]{total_failed}[/bold red]",
        f"[bold red]{total_errors}[/bold red]",
        f"[bold]{total_time:.2f}s[/bold]",
    )

    console.print()
    console.print(table)
    console.print()

    # Overall summary
    if all(r.success for r in results):
        console.print(
            Panel(
                f"[green bold]✅ All {len(results)} test modules passed![/green bold]\n"
                f"Total: {total_passed}/{total_tests} tests passed in {total_time:.2f}s",
                border_style="green",
            )
        )
    else:
        failed_modules = [r.module for r in results if not r.success]
        console.print(
            Panel(
                f"[red bold]❌ {len(failed_modules)} module(s) failed[/red bold]\n"
                f"Failed: {', '.join(failed_modules)}\n"
                f"Total: {total_passed}/{total_tests} tests passed",
                border_style="red",
            )
        )


def print_results_plain(results: List[TestResult], total_time: float):
    """Print test results using plain text formatting."""
    print("\n" + "=" * 80)
    print("CLI TEST RESULTS")
    print("=" * 80)
    print(f"{'Module':<30} {'Status':<10} {'Tests':<8} {'Passed':<8} {'Failed':<8} {'Time':<8}")
    print("-" * 80)

    total_tests = 0
    total_passed = 0
    total_failed = 0
    total_errors = 0

    for result in results:
        status = "PASS" if result.success else "FAIL"
        print(
            f"{result.module.replace('test_', ''):<30} {status:<10} {result.total:<8} {result.passed:<8} {result.failed:<8} {result.time:.2f}s"
        )

        total_tests += result.total
        total_passed += result.passed
        total_failed += result.failed
        total_errors += result.errors

    print("-" * 80)
    overall_status = "ALL PASS" if all(r.success for r in results) else "SOME FAILED"
    print(
        f"{'TOTAL':<30} {overall_status:<10} {total_tests:<8} {total_passed:<8} {total_failed:<8} {total_time:.2f}s"
    )
    print("=" * 80)

    if all(r.success for r in results):
        print(f"\n✅ All {len(results)} test modules passed!")
    else:
        failed_modules = [r.module for r in results if not r.success]
        print(f"\n❌ {len(failed_modules)} module(s) failed: {', '.join(failed_modules)}")
    print()


def list_test_modules():
    """List all available test modules."""
    if HAS_RICH:
        table = Table(title="Available Test Modules", box=box.ROUNDED)
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Module", style="green")
        table.add_column("Command", style="yellow")

        for i, module in enumerate(TEST_MODULES, 1):
            command = module.replace("test_", "").replace("_", "-")
            table.add_row(str(i), module, command)

        console.print(table)
    else:
        print("\nAvailable Test Modules:")
        print("-" * 50)
        for i, module in enumerate(TEST_MODULES, 1):
            command = module.replace("test_", "").replace("_", "-")
            print(f"{i:3}. {module:<30} ({command})")
        print()


def main():
    """Main test runner."""
    parser = argparse.ArgumentParser(description="Run CLI command tests")
    parser.add_argument(
        "modules", nargs="*", help="Specific test modules to run (e.g., validate, plan)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("-l", "--list", action="store_true", help="List all test modules")
    parser.add_argument("--no-rich", action="store_true", help="Disable rich formatting")

    args = parser.parse_args()

    # Handle list command
    if args.list:
        list_test_modules()
        return 0

    # Determine which modules to run
    if args.modules:
        modules_to_run = []
        for mod in args.modules:
            # Allow both 'validate' and 'test_validate' formats
            test_mod = mod if mod.startswith("test_") else f"test_{mod}"
            if test_mod in TEST_MODULES:
                modules_to_run.append(test_mod)
            else:
                print(f"Warning: Unknown test module '{mod}', skipping...")

        if not modules_to_run:
            print("Error: No valid test modules specified")
            return 1
    else:
        modules_to_run = TEST_MODULES

    # Run tests
    verbosity = 2 if args.verbose else 1
    use_rich = HAS_RICH and not args.no_rich

    if use_rich:
        console.print(
            Panel(
                f"[bold cyan]Running {len(modules_to_run)} test module(s)[/bold cyan]",
                border_style="cyan",
            )
        )
    else:
        print(f"\nRunning {len(modules_to_run)} test module(s)...\n")

    start_time = time.time()
    results = []

    for module in modules_to_run:
        if use_rich and not args.verbose:
            console.print(f"[cyan]Running {module}...[/cyan]", end="")
        elif not args.verbose:
            print(f"Running {module}...", end="")

        result = run_test_module(module, verbosity)
        results.append(result)

        if use_rich and not args.verbose:
            status = "[green]✅ PASS[/green]" if result.success else "[red]❌ FAIL[/red]"
            console.print(f" {status}")
        elif not args.verbose:
            status = "✅ PASS" if result.success else "❌ FAIL"
            print(f" {status}")

    total_time = time.time() - start_time

    # Print results
    if use_rich:
        print_results_rich(results, total_time)
    else:
        print_results_plain(results, total_time)

    # Return exit code
    return 0 if all(r.success for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
