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
FLUID Build - Consolidated Test Runner

Main test orchestrator that runs all test categories and integrates with 'fluid admin test'.
Consolidates all previous test scripts into a clean, organized structure with beautiful logging.
"""

import argparse
import json
import os
import platform
import queue
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def find_project_root() -> Path:
    """Find the FLUID Build project root"""
    current = Path(__file__).resolve().parent.parent

    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists() and (parent / "fluid_build").exists():
            return parent

    return current


# Rich imports for enhanced output (optional)
try:
    import logging

    from rich import box
    from rich.align import Align
    from rich.columns import Columns
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.logging import RichHandler
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskID,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.status import Status
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.traceback import install
    from rich.tree import Tree

    # Install rich traceback handler
    install()

    RICH_AVAILABLE = True
    console = Console(width=120, color_system="truecolor")
except ImportError:
    RICH_AVAILABLE = False
    console = None
    import logging

    console = None


class ConsolidatedTestRunner:
    """Main test runner that orchestrates all test categories with beautiful console output"""

    def __init__(self, project_root: Path, output_dir: Path = None, quick_mode: bool = False):
        self.project_root = project_root
        self.quick_mode = quick_mode
        self.include_all = False  # Will be set when run_all_tests is called

        # Create timestamped output directory with proper structure
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = str(uuid.uuid4())[:8]
        self.output_dir = output_dir or (
            project_root / "runtime" / "test_sessions" / f"{timestamp}_{self.session_id}"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories for organized storage
        (self.output_dir / "logs").mkdir(exist_ok=True)
        (self.output_dir / "reports").mkdir(exist_ok=True)
        (self.output_dir / "artifacts").mkdir(exist_ok=True)
        (self.output_dir / "performance").mkdir(exist_ok=True)

        # Set up enhanced logging with rich formatting
        self.setup_logging()

        # Progress tracking
        self.current_test = None
        self.test_start_time = None
        self.overall_start_time = None
        self.test_queue = queue.Queue()

        # Individual test categories (mapped to existing files)
        self.test_modules = {
            "basic_cli": "tests.individual.test_basic",
            "advanced": "tests.individual.test_advanced",
            "validation": "tests.individual.test_validation",
            "planning": "tests.individual.test_planning",
            "apply": "tests.individual.test_apply",
            "visualization": "tests.individual.test_visualization",
            "documentation": "tests.individual.test_documentation",
            "cli_viz_graph": "tests.individual.test_cli_viz_graph",
            "cli_core_commands": "tests.individual.test_cli_core_commands",
            "cli_config_commands": "tests.individual.test_cli_config_commands",
            "market": "tests.individual.test_market",
            "auth": "tests.individual.test_auth",
        }

        # Comprehensive test suites (based on original archived scripts)
        self.comprehensive_modules = {
            "complete_cli_validation": "tests.comprehensive.test_complete_cli_validation",
            "complete_cli_audit": "tests.comprehensive.test_complete_cli_audit",
            "complete_system_diagnostics": "tests.comprehensive.test_complete_system_diagnostics",
            "complete_test_matrix": "tests.comprehensive.test_complete_matrix",
            "complete_orchestration": "tests.comprehensive.test_complete_orchestration",
            "cli_matrix": "tests.comprehensive.test_cli_matrix",
            "edge_cases": "tests.comprehensive.test_edge_cases",
            "performance": "tests.comprehensive.test_performance",
            "security": "tests.comprehensive.test_security",
            "compatibility": "tests.comprehensive.test_compatibility",
            "cli_comprehensive": "tests.comprehensive.test_cli_comprehensive",
        }

    def setup_logging(self):
        """Set up enhanced logging with rich formatting"""
        log_file = self.output_dir / "logs" / "test_session.log"

        # Configure logging
        if RICH_AVAILABLE:
            # Rich handler for console output
            rich_handler = RichHandler(
                console=console,
                show_time=True,
                show_level=True,
                show_path=False,
                rich_tracebacks=True,
            )
            rich_handler.setLevel(logging.INFO)

            # File handler for detailed logs
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.DEBUG)
            file_formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            file_handler.setFormatter(file_formatter)

            # Configure root logger
            logging.basicConfig(
                level=logging.DEBUG,
                handlers=[rich_handler, file_handler],
                format="%(message)s",
                datefmt="[%X]",
            )
        else:
            # Fallback to standard logging
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(levelname)s - %(message)s",
                handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
            )

        self.logger = logging.getLogger(f"fluid_test_session_{self.session_id}")

    def create_fluid_banner(self):
        """Create beautiful FLUID Build branded banner"""
        if not RICH_AVAILABLE:
            return "🌊 FLUID Build Test Suite"

        banner_text = """
    ███████╗██╗     ██╗   ██╗██╗██████╗     ██████╗ ██╗   ██╗██╗██╗     ██████╗ 
    ██╔════╝██║     ██║   ██║██║██╔══██╗    ██╔══██╗██║   ██║██║██║     ██╔══██╗
    █████╗  ██║     ██║   ██║██║██║  ██║    ██████╔╝██║   ██║██║██║     ██║  ██║
    ██╔══╝  ██║     ██║   ██║██║██║  ██║    ██╔══██╗██║   ██║██║██║     ██║  ██║
    ██║     ███████╗╚██████╔╝██║██████╔╝    ██████╔╝╚██████╔╝██║███████╗██████╔╝
    ╚═╝     ╚══════╝ ╚═════╝ ╚═╝╚═════╝     ╚═════╝  ╚═════╝ ╚═╝╚══════╝╚═════╝ 
        """

        content = Text(banner_text, style="bold cyan")
        content.append("\n🧪 Comprehensive Test Suite", style="bold white")
        content.append(f"\n📁 Session: {self.session_id}", style="dim white")
        content.append(
            f"\n⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", style="dim white"
        )
        content.append(f"\n📊 Output: {self.output_dir}", style="dim white")

        return Panel(
            Align.center(content),
            title="🌊 FLUID Build Testing Framework",
            subtitle="Building the future of data workflows",
            border_style="cyan",
            box=box.DOUBLE,
        )

    def create_test_status_table(self, results: Dict[str, Any], current_test: str = None):
        """Create a beautiful status table showing test progress"""
        if not RICH_AVAILABLE:
            return None

        table = Table(
            title="🧪 Test Execution Status",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Test Category", style="white", width=25)
        table.add_column("Status", style="white", width=12)
        table.add_column("Duration", style="white", width=10)
        table.add_column("Result", style="white", width=15)
        table.add_column("Details", style="dim white", width=30)

        # Get all modules for display
        all_modules = {**self.test_modules, **self.comprehensive_modules}

        for test_name, module_path in all_modules.items():
            # Status indicators
            if test_name == current_test:
                status = "🔄 Running"
                status_style = "bold yellow"
            elif test_name in results:
                result = results[test_name]
                if result.get("error"):
                    status = "❌ Failed"
                    status_style = "bold red"
                elif result.get("passed", 0) > 0:
                    status = "✅ Passed"
                    status_style = "bold green"
                else:
                    status = "⚠️  Unknown"
                    status_style = "bold yellow"
            else:
                status = "⏳ Pending"
                status_style = "dim white"

            # Duration
            duration = ""
            if test_name in results:
                result = results[test_name]
                duration = f"{result.get('duration', 0):.2f}s"
            elif test_name == current_test and self.test_start_time:
                duration = f"{time.time() - self.test_start_time:.1f}s"

            # Result details
            result_details = ""
            details = ""
            if test_name in results:
                result = results[test_name]
                passed = result.get("passed", 0)
                failed = result.get("failed", 0)
                result_details = f"P:{passed} F:{failed}"

                if result.get("error"):
                    details = (
                        result["error"][:25] + "..."
                        if len(result["error"]) > 25
                        else result["error"]
                    )
                else:
                    details = f"{passed + failed} tests executed"

            table.add_row(
                test_name.replace("_", " ").title(),
                Text(status, style=status_style),
                duration,
                result_details,
                details,
            )

        return table

    def run_test_module(self, module_name: str) -> dict:
        """Run a specific test module"""
        # Check both individual and comprehensive modules
        all_modules = {**self.test_modules, **self.comprehensive_modules}

        if module_name not in all_modules:
            self.logger.error(f"Unknown test module: {module_name}")
            return {"passed": 0, "failed": 1, "error": f"Unknown module: {module_name}"}

        # Map module names to directory structures
        # Test file mapping - updated to match existing files
        test_file_map = {
            # Individual test categories (existing files)
            "basic_cli": "individual/test_basic.py",
            "advanced": "individual/test_advanced.py",
            "validation": "individual/test_validation.py",
            "planning": "individual/test_planning.py",
            "apply": "individual/test_apply.py",
            "visualization": "individual/test_visualization.py",
            "documentation": "individual/test_documentation.py",
            "market": "individual/test_market.py",
            "auth": "individual/test_auth.py",
            # Comprehensive test suites (existing files)
            "complete_cli_validation": "comprehensive/test_complete_cli_validation.py",
            "complete_cli_audit": "comprehensive/test_complete_cli_audit.py",
            "complete_system_diagnostics": "comprehensive/test_complete_system_diagnostics.py",
            "complete_test_matrix": "comprehensive/test_complete_matrix.py",
            "complete_orchestration": "comprehensive/test_complete_orchestration.py",
            "cli_matrix": "comprehensive/test_cli_matrix.py",
            "edge_cases": "comprehensive/test_edge_cases.py",
            "performance": "comprehensive/test_performance.py",
            "security": "comprehensive/test_security.py",
            "compatibility": "comprehensive/test_compatibility.py",
            # Legacy mappings for backward compatibility
            "basic": "individual/test_basic.py",
            "system_diagnostics": "comprehensive/test_system_diagnostics.py",
        }

        test_file_path = test_file_map.get(module_name)
        if not test_file_path:
            self.logger.error(f"Test file not mapped for module: {module_name}")
            return {"passed": 0, "failed": 1, "error": f"Test file not mapped: {module_name}"}

        test_file = self.project_root / "tests" / test_file_path
        if not test_file.exists():
            error_msg = f"Test file not found: {test_file}"
            self.logger.error(error_msg)
            return {"passed": 0, "failed": 1, "error": error_msg, "duration": 0}

        # Update current test tracking
        self.current_test = module_name
        self.test_start_time = time.time()

        # Beautiful logging for test start
        if RICH_AVAILABLE:
            console.print(
                f"\n🚀 [bold cyan]Starting test:[/bold cyan] [bold white]{module_name.replace('_', ' ').title()}[/bold white]"
            )
            console.print(f"📁 [dim]Test file:[/dim] {test_file}")
        else:
            self.logger.info(f"🧪 Running {module_name} tests...")

        # Create test-specific output directory
        test_output_dir = self.output_dir / "artifacts" / module_name
        test_output_dir.mkdir(exist_ok=True)

        try:
            # Run the test module with enhanced environment
            env = dict(os.environ)
            env["FLUID_TEST_OUTPUT_DIR"] = str(test_output_dir)
            env["FLUID_TEST_SESSION_ID"] = self.session_id
            env["FLUID_TEST_NAME"] = module_name
            # Set UTF-8 encoding for Windows compatibility
            env["PYTHONIOENCODING"] = "utf-8"

            if RICH_AVAILABLE:
                with Status(f"[bold yellow]Executing {module_name}...", spinner="dots"):
                    result = subprocess.run(
                        [sys.executable, str(test_file)],
                        cwd=self.project_root,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=1800,  # 30 minutes timeout
                        env=env,
                    )
            else:
                result = subprocess.run(
                    [sys.executable, str(test_file)],
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1800,
                    env=env,
                )

            duration = time.time() - self.test_start_time

            # Save detailed output
            output_file = test_output_dir / "execution.log"
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(f"Test Module: {module_name}\n")
                f.write(f"Start Time: {datetime.fromtimestamp(self.test_start_time).isoformat()}\n")
                f.write(f"Duration: {duration:.2f}s\n")
                f.write(f"Exit Code: {result.returncode}\n")
                f.write(f"Command: {sys.executable} {test_file}\n")
                f.write("\n=== STDOUT ===\n")
                f.write(result.stdout)
                f.write("\n=== STDERR ===\n")
                f.write(result.stderr)

            # Parse results and provide enhanced feedback
            if result.returncode == 0:
                # Success - Parse detailed output for more information
                stdout_lines = result.stdout.split("\n")

                # Try to parse the Results line first (most accurate)
                passed_count = 0
                failed_count = 0
                for line in stdout_lines:
                    if "Results:" in line and "PASSED" in line:
                        # Parse "Results: X PASSED, Y FAILED" format
                        import re

                        passed_match = re.search(r"(\d+)\s+PASSED", line)
                        failed_match = re.search(r"(\d+)\s+FAILED", line)
                        if passed_match:
                            passed_count = int(passed_match.group(1))
                        if failed_match:
                            failed_count = int(failed_match.group(1))
                        break

                # Fallback: count emoji indicators if no Results line found
                if passed_count == 0 and failed_count == 0:
                    passed_count = result.stdout.count("✅")
                    failed_count = result.stdout.count("❌")

                if passed_count == 0 and failed_count == 0:
                    passed_count = 1  # Assume success if no specific indicators

                # Extract individual test details from output with enhanced parsing
                test_details = []
                current_test = None
                for line in result.stdout.split("\n"):
                    line = line.strip()
                    if "Testing:" in line:
                        current_test = line.replace("Testing:", "").strip()
                    elif "✅" in line or "PASS" in line:
                        if current_test:
                            test_details.append(
                                {"name": current_test, "status": "PASS", "details": line}
                            )
                        else:
                            test_details.append(
                                {"name": "Unknown", "status": "PASS", "details": line}
                            )
                    elif "❌" in line or "FAIL" in line:
                        if current_test:
                            test_details.append(
                                {"name": current_test, "status": "FAIL", "details": line}
                            )
                        else:
                            test_details.append(
                                {"name": "Unknown", "status": "FAIL", "details": line}
                            )

                if RICH_AVAILABLE:
                    console.print(
                        f"✅ [bold green]{module_name}[/bold green] completed successfully in [bold]{duration:.2f}s[/bold]"
                    )
                    console.print(
                        f"   📊 Results: [green]{passed_count} passed[/green], [red]{failed_count} failed[/red]"
                    )
                    if test_details:
                        console.print(f"   📋 Individual tests: {len(test_details)} executed")

                return {
                    "passed": passed_count,
                    "failed": failed_count,
                    "duration": duration,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "output_file": output_file,
                    "test_details": test_details,
                    "execution_summary": {
                        "total_tests": len(test_details),
                        "success_rate": (passed_count / max(passed_count + failed_count, 1)) * 100,
                        "avg_duration_per_test": duration / max(len(test_details), 1),
                    },
                }

            else:
                # Failed
                duration = time.time() - self.test_start_time
                error_msg = result.stderr or "Test execution failed"

                # Try to parse any test details from failed output
                test_details = []
                if result.stdout:
                    for line in result.stdout.split("\n"):
                        line = line.strip()
                        if "❌" in line or "FAIL" in line:
                            test_details.append(
                                {"name": "Unknown", "status": "FAIL", "details": line}
                            )

                if RICH_AVAILABLE:
                    console.print(
                        f"❌ [bold red]{module_name}[/bold red] failed in [bold]{duration:.2f}s[/bold]"
                    )
                    console.print(f"   💥 Error: [red]{error_msg}[/red]")

                return {
                    "passed": 0,
                    "failed": 1,
                    "error": error_msg,
                    "duration": duration,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "output_file": output_file,
                    "error_details": (
                        [line.strip() for line in result.stderr.split("\n") if line.strip()]
                        if result.stderr
                        else []
                    ),
                    "execution_summary": {
                        "total_tests": 1,
                        "success_rate": 0,
                        "avg_duration_per_test": duration,
                    },
                }

        except subprocess.TimeoutExpired:
            duration = time.time() - self.test_start_time
            if RICH_AVAILABLE:
                console.print(
                    f"⏰ [bold yellow]{module_name}[/bold yellow] timed out after [bold]{duration:.2f}s[/bold]"
                )

            return {
                "passed": 0,
                "failed": 1,
                "error": f"Test timed out after {duration:.2f}s",
                "duration": duration,
                "stdout": "",
                "stderr": "Test execution timed out",
                "output_file": output_file,
                "execution_summary": {
                    "total_tests": 1,
                    "success_rate": 0,
                    "avg_duration_per_test": duration,
                },
            }

        except Exception as e:
            duration = time.time() - self.test_start_time
            if RICH_AVAILABLE:
                console.print(
                    f"💥 [bold red]{module_name}[/bold red] crashed in [bold]{duration:.2f}s[/bold]"
                )
                console.print(f"   💥 Error: [red]{str(e)}[/red]")

            return {
                "passed": 0,
                "failed": 1,
                "error": str(e),
                "duration": duration,
                "stdout": "",
                "stderr": str(e),
                "output_file": output_file,
                "execution_summary": {
                    "total_tests": 1,
                    "success_rate": 0,
                    "avg_duration_per_test": duration,
                },
            }

        finally:
            self.current_test = None


class ConsolidatedTestRunner:
    """Enhanced test runner with FLUID branding and organized output"""

    def __init__(self, project_root: Path, output_dir: Path = None, quick_mode: bool = False):
        self.project_root = project_root
        self.quick_mode = quick_mode

        # Create timestamped output directory with proper structure
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = str(uuid.uuid4())[:8]
        self.output_dir = output_dir or (
            project_root / "runtime" / "test_sessions" / f"{timestamp}_{self.session_id}"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories for organized storage
        (self.output_dir / "logs").mkdir(exist_ok=True)
        (self.output_dir / "reports").mkdir(exist_ok=True)
        (self.output_dir / "artifacts").mkdir(exist_ok=True)
        (self.output_dir / "performance").mkdir(exist_ok=True)

        # Set up enhanced logging with rich formatting
        if RICH_AVAILABLE:
            self.logger = logging.getLogger(f"fluid_test_{self.session_id}")
            self.logger.setLevel(logging.INFO)

            # Rich handler for console
            rich_handler = RichHandler(rich_tracebacks=True, show_path=False)
            rich_handler.setLevel(logging.INFO)
            formatter = logging.Formatter("%(message)s")
            rich_handler.setFormatter(formatter)
            self.logger.addHandler(rich_handler)

        self.test_results = {}
        self.start_time = datetime.now()

    def get_available_test_modules(self) -> Dict[str, str]:
        """Get available test modules by category"""
        test_modules = {
            # Individual tests
            "basic_cli": "tests.individual.test_basic",
            "advanced": "tests.individual.test_advanced",
            "validation": "tests.individual.test_validation",
            "planning": "tests.individual.test_planning",
            "apply": "tests.individual.test_apply",
            "visualization": "tests.individual.test_visualization",
            "documentation": "tests.individual.test_documentation",
            "market": "tests.individual.test_market",
            "auth": "tests.individual.test_auth",
            # New CLI test modules
            "cli_viz_graph": "tests.individual.test_cli_viz_graph",
            "cli_core_commands": "tests.individual.test_cli_core_commands",
            "cli_config_commands": "tests.individual.test_cli_config_commands",
            # Plugin system tests
            "plugin_system": "tests.plugins.test_plugin_system",
            # Comprehensive tests
            "complete_cli_validation": "tests.comprehensive.test_complete_cli_validation",
            "complete_cli_audit": "tests.comprehensive.test_complete_cli_audit",
            "complete_system_diagnostics": "tests.comprehensive.test_complete_system_diagnostics",
            "complete_test_matrix": "tests.comprehensive.test_complete_matrix",
            "complete_orchestration": "tests.comprehensive.test_complete_orchestration",
            "cli_matrix": "tests.comprehensive.test_cli_matrix",
            "edge_cases": "tests.comprehensive.test_edge_cases",
            "performance": "tests.comprehensive.test_performance",
            "security": "tests.comprehensive.test_security",
            "compatibility": "tests.comprehensive.test_compatibility",
        }
        return test_modules

    def run_all_tests(self, categories: List[str] = None, run_all: bool = False) -> Dict[str, Any]:
        """Run all tests or specified categories"""

        # Set the include_all flag for use in reporting
        self.include_all = run_all

        # Print FLUID banner
        try:
            self.print_banner()
        except Exception:
            # Fallback if Rich console fails
            print("\n" + "=" * 80)
            print("FLUID Build Consolidated Test Suite")
            print(f"Session ID: {self.session_id}")
            print(f"Output Dir: {self.output_dir}")
            print(f"Quick Mode: {self.quick_mode}")
            print("=" * 80 + "\n")

        available_modules = self.get_available_test_modules()

        if categories:
            # Filter to requested categories
            test_modules = {k: v for k, v in available_modules.items() if k in categories}
            if not test_modules:
                if RICH_AVAILABLE:
                    console.print(f"[red]❌ No valid test categories found in: {categories}[/red]")
                return {}
        elif run_all:
            # Run ALL tests when --all flag is used
            test_modules = available_modules
            if RICH_AVAILABLE:
                console.print(
                    "[yellow]⚠️  Running ALL test categories (may take time). Use Ctrl+C to cancel.[/yellow]"
                )
        else:
            # Default to core working tests instead of ALL tests to prevent timeouts
            if self.quick_mode:
                # Quick mode - just basic tests including new commands
                core_tests = ["basic_cli", "validation", "market", "auth"]
            else:
                # Standard mode - core individual tests + one comprehensive test
                core_tests = [
                    "basic_cli",
                    "advanced",
                    "validation",
                    "planning",
                    "apply",
                    "market",
                    "auth",  # Include new command tests
                    "complete_cli_validation",  # Include one comprehensive test that works well
                ]

            test_modules = {k: v for k, v in available_modules.items() if k in core_tests}
            if RICH_AVAILABLE:
                console.print(
                    "[yellow]ℹ️  Running core test suite. Use --all for all tests or --categories to specify others.[/yellow]"
                )

        # Print execution plan
        self.print_execution_plan(test_modules)

        results = {}
        total_start_time = time.time()

        # Run tests
        if RICH_AVAILABLE:
            with Progress(
                SpinnerColumn(),
                TextColumn(
                    "[bold blue]🧪 Running FLUID Build Tests ({task.description})[/bold blue]"
                ),
                BarColumn(bar_width=40),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:

                task = progress.add_task("Initializing...", total=len(test_modules))

                for i, (category, module_path) in enumerate(test_modules.items(), 1):
                    progress.update(task, description=category.replace("_", " ").title())
                    try:
                        console.print(
                            f"\nStarting test: [bold cyan]{category.replace('_', ' ').title()}[/bold cyan]"
                        )
                    except (UnicodeEncodeError, Exception):
                        print(f"\nStarting test: {category.replace('_', ' ').title()}")

                    # Run the test
                    result = self.run_test_module(category, module_path)
                    results[category] = result

                    progress.advance(task)

                    # Short pause for better UX
                    time.sleep(0.1)
        else:
            # Fallback without rich
            for i, (category, module_path) in enumerate(test_modules.items(), 1):
                print(
                    f"\nStarting test {i}/{len(test_modules)}: {category.replace('_', ' ').title()}"
                )

                # Run the test
                result = self.run_test_module(category, module_path)
                results[category] = result

        # Calculate summary
        total_duration = time.time() - total_start_time
        summary = self.calculate_summary(results, total_duration)

        # Generate reports
        self.generate_session_summary(summary)
        self.generate_enhanced_html_report(summary)

        if RICH_AVAILABLE:
            self.logger.info(
                f"Enhanced HTML report generated: {self.output_dir / 'reports' / 'enhanced_test_report.html'}"
            )

        return summary

    def run_test_module(self, category: str, module_path: str) -> Dict[str, Any]:
        """Run a single test module"""

        # Create category-specific output directory
        category_output = self.output_dir / "artifacts" / category
        category_output.mkdir(parents=True, exist_ok=True)

        # Convert module path to file path
        file_path = self.project_root / (module_path.replace(".", "/") + ".py")

        if RICH_AVAILABLE:
            console.print(f"📁 Test file: [dim]{file_path}[/dim]")

        if not file_path.exists():
            return {
                "passed": 0,
                "failed": 1,
                "error": f"Test file not found: {file_path}",
                "duration": 0,
                "stdout": "",
                "stderr": f"Test file not found: {file_path}",
                "output_file": str(category_output / "execution.log"),
            }

        # Run the test
        start_time = time.time()
        try:
            result = subprocess.run(
                [sys.executable, str(file_path)],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,  # 5 minute timeout
            )
            duration = time.time() - start_time

            # Save output to file
            output_file = category_output / "execution.log"
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(f"Command: {sys.executable} {file_path}\n")
                f.write(f"Duration: {duration:.2f}s\n")
                f.write(f"Return code: {result.returncode}\n\n")
                f.write("STDOUT:\n")
                f.write(result.stdout)
                f.write("\nSTDERR:\n")
                f.write(result.stderr)

            if result.returncode == 0:
                # Success - Parse output for statistics
                stdout_lines = result.stdout.split("\n")

                # Try to parse the Results line first (most accurate)
                passed_count = 0
                failed_count = 0
                for line in stdout_lines:
                    if "Results:" in line and "PASSED" in line:
                        # Parse "Results: X PASSED, Y FAILED" format
                        import re

                        passed_match = re.search(r"(\d+)\s+PASSED", line)
                        failed_match = re.search(r"(\d+)\s+FAILED", line)
                        if passed_match:
                            passed_count = int(passed_match.group(1))
                        if failed_match:
                            failed_count = int(failed_match.group(1))
                        break

                # Fallback: count emoji indicators if no Results line found
                if passed_count == 0 and failed_count == 0:
                    passed_count = result.stdout.count("✅")
                    failed_count = result.stdout.count("❌")

                if passed_count == 0 and failed_count == 0:
                    passed_count = 1  # Assume success if no specific indicators

                # Extract individual test details from output
                test_details = []
                current_test = None
                for line in stdout_lines:
                    line = line.strip()
                    if "Testing:" in line:
                        current_test = line.replace("Testing:", "").strip()
                    elif "✅" in line or "PASS" in line:
                        if current_test:
                            test_details.append(
                                {"name": current_test, "status": "PASS", "details": line}
                            )
                        else:
                            test_details.append(
                                {"name": "Unknown", "status": "PASS", "details": line}
                            )
                    elif "❌" in line or "FAIL" in line:
                        if current_test:
                            test_details.append(
                                {"name": current_test, "status": "FAIL", "details": line}
                            )
                        else:
                            test_details.append(
                                {"name": "Unknown", "status": "FAIL", "details": line}
                            )

                if RICH_AVAILABLE:
                    console.print(
                        f"✅ [bold green]{category.replace('_', ' ').title()}[/bold green] completed successfully in [bold]{duration:.2f}s[/bold]"
                    )
                    console.print(
                        f"   📊 Results: [green]{passed_count} passed[/green], [red]{failed_count} failed[/red]"
                    )
                    if test_details:
                        console.print(f"   📋 Individual tests: {len(test_details)} executed")

                return {
                    "passed": passed_count,
                    "failed": failed_count,
                    "duration": duration,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "output_file": str(output_file),
                    "test_details": test_details,
                    "execution_summary": {
                        "total_tests": len(test_details),
                        "success_rate": (passed_count / max(passed_count + failed_count, 1)) * 100,
                        "avg_duration_per_test": duration / max(len(test_details), 1),
                    },
                }

            else:
                # Failed
                error_msg = result.stderr or "Test execution failed"

                # Try to parse any test details from failed output
                test_details = []
                if result.stdout:
                    for line in result.stdout.split("\n"):
                        line = line.strip()
                        if "❌" in line or "FAIL" in line:
                            test_details.append(
                                {"name": "Unknown", "status": "FAIL", "details": line}
                            )

                if RICH_AVAILABLE:
                    console.print(
                        f"❌ [bold red]{category.replace('_', ' ').title()}[/bold red] failed in [bold]{duration:.2f}s[/bold]"
                    )
                    console.print(f"   💥 Error: [red]{error_msg}[/red]")

                return {
                    "passed": 0,
                    "failed": 1,
                    "error": error_msg,
                    "duration": duration,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "output_file": str(output_file),
                    "error_details": (
                        [line.strip() for line in result.stderr.split("\n") if line.strip()]
                        if result.stderr
                        else []
                    ),
                    "execution_summary": {
                        "total_tests": 1,
                        "success_rate": 0,
                        "avg_duration_per_test": duration,
                    },
                }

        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            if RICH_AVAILABLE:
                console.print(
                    f"⏰ [bold yellow]{category.replace('_', ' ').title()}[/bold yellow] timed out after [bold]{duration:.2f}s[/bold]"
                )

            return {
                "passed": 0,
                "failed": 1,
                "error": f"Test timed out after {duration:.2f}s",
                "duration": duration,
                "stdout": "",
                "stderr": "Test execution timed out",
                "output_file": str(category_output / "execution.log"),
                "execution_summary": {
                    "total_tests": 1,
                    "success_rate": 0,
                    "avg_duration_per_test": duration,
                },
            }

        except Exception as e:
            duration = time.time() - start_time
            if RICH_AVAILABLE:
                console.print(
                    f"💥 [bold red]{category.replace('_', ' ').title()}[/bold red] crashed in [bold]{duration:.2f}s[/bold]"
                )
                console.print(f"   💥 Error: [red]{str(e)}[/red]")

            return {
                "passed": 0,
                "failed": 1,
                "error": str(e),
                "duration": duration,
                "stdout": "",
                "stderr": str(e),
                "output_file": str(category_output / "execution.log"),
                "execution_summary": {
                    "total_tests": 1,
                    "success_rate": 0,
                    "avg_duration_per_test": duration,
                },
            }

    def print_banner(self):
        """Print the FLUID Forge banner"""
        banner_text = """
================================================================================
                              FLUID FORGE
                            Testing Framework
================================================================================
  Session:  {session_id}
  Started:  {timestamp}
  Output:   {output_dir}
================================================================================
""".format(
            session_id=self.session_id,
            timestamp=self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            output_dir=str(self.output_dir),
        )
        print(banner_text)

    def print_execution_plan(self, test_modules: Dict[str, str]):
        """Print test execution plan"""
        if not RICH_AVAILABLE:
            print("Test Execution Plan:")
            for category in test_modules.keys():
                print(f"  - {category}")
            return

        table = Table(title="📋 Test Execution Plan", box=box.ROUNDED)
        table.add_column("Test Category", style="cyan", no_wrap=True)
        table.add_column("Type", style="magenta")
        table.add_column("Description", style="green")

        for category in test_modules.keys():
            test_type = "Comprehensive" if category.startswith("complete_") else "Individual"
            description = (
                "End-to-end test suite" if test_type == "Comprehensive" else "Focused test suite"
            )
            table.add_row(category.replace("_", " ").title(), test_type, description)

        try:
            console.print("\n")
            console.print(table)
            console.print("\n")
        except Exception:
            # Fallback if console fails
            print("\nTest Execution Plan:")
            for category in test_modules.keys():
                print(f"  - {category}")
            print()

    def calculate_summary(self, results: Dict[str, Any], total_duration: float) -> Dict[str, Any]:
        """Calculate test summary statistics with enhanced details"""
        total_passed = sum(r.get("passed", 0) for r in results.values())
        total_failed = sum(r.get("failed", 0) for r in results.values())
        total_tests = len(results)
        success_rate = (total_passed / max(total_passed + total_failed, 1)) * 100

        # Calculate additional analytics
        failed_categories = [cat for cat, result in results.items() if result.get("failed", 0) > 0]
        avg_duration = total_duration / total_tests if total_tests > 0 else 0
        fastest_test = (
            min(results.items(), key=lambda x: x[1].get("duration", float("inf")))
            if results
            else None
        )
        slowest_test = (
            max(results.items(), key=lambda x: x[1].get("duration", 0)) if results else None
        )

        # Test distribution by type
        individual_tests = sum(1 for cat in results.keys() if not cat.startswith("complete_"))
        comprehensive_tests = sum(1 for cat in results.keys() if cat.startswith("complete_"))

        return {
            "session_id": self.session_id,
            "start_time": self.start_time.isoformat(),
            "end_time": datetime.now().isoformat(),
            "total_tests": total_tests,
            "total_passed": total_passed,
            "total_failed": total_failed,
            "success_rate": success_rate,
            "total_duration": total_duration,
            "average_duration": avg_duration,
            "failed_categories": failed_categories,
            "test_distribution": {
                "individual_tests": individual_tests,
                "comprehensive_tests": comprehensive_tests,
            },
            "performance_insights": {
                "fastest_category": fastest_test[0] if fastest_test else None,
                "fastest_duration": fastest_test[1].get("duration", 0) if fastest_test else 0,
                "slowest_category": slowest_test[0] if slowest_test else None,
                "slowest_duration": slowest_test[1].get("duration", 0) if slowest_test else 0,
            },
            "categories": results,
            "output_directory": str(self.output_dir),
            "project_root": str(self.project_root),
            "environment_info": {
                "python_version": sys.version,
                "platform": sys.platform,
                "quick_mode": self.quick_mode,
            },
        }

    def generate_session_summary(self, summary: Dict[str, Any]):
        """Generate JSON summary file"""
        summary_file = self.output_dir / "reports" / "session_summary.json"
        with open(summary_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    def generate_enhanced_html_report(self, summary: Dict[str, Any]):
        """Generate enhanced HTML report with FLUID branding and detailed test information"""

        # Calculate additional statistics
        [cat for cat, result in summary["categories"].items() if result.get("failed", 0) > 0]
        avg_duration = (
            summary["total_duration"] / summary["total_tests"] if summary["total_tests"] > 0 else 0
        )

        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FLUID Build Test Report - {summary['session_id']}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', sans-serif; 
            line-height: 1.6; 
            color: #1f2937; 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 2rem;
        }}
        
        .container {{ 
            max-width: 1200px; 
            margin: 0 auto; 
            background: white; 
            border-radius: 12px; 
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        
        .header {{ 
            background: linear-gradient(135deg, #0891b2 0%, #0e7490 100%);
            color: white; 
            padding: 3rem 2rem; 
            text-align: center; 
            position: relative;
        }}
        
        .header::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="20" cy="20" r="2" fill="rgba(255,255,255,0.1)"/><circle cx="80" cy="40" r="1" fill="rgba(255,255,255,0.1)"/><circle cx="40" cy="80" r="1.5" fill="rgba(255,255,255,0.1)"/></svg>');
        }}
        
        .header h1 {{ 
            font-size: 2.5rem; 
            margin-bottom: 0.5rem; 
            position: relative;
            z-index: 1;
        }}
        
        .header .subtitle {{ 
            opacity: 0.9; 
            font-size: 1.1rem;
            position: relative;
            z-index: 1;
        }}
        
        .content {{ padding: 2rem; }}
        
        .summary-grid {{ 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
            gap: 1.5rem; 
            margin-bottom: 3rem; 
        }}
        
        .summary-card {{ 
            background: #f8fafc; 
            padding: 1.5rem; 
            border-radius: 8px; 
            border-left: 4px solid;
            text-align: center;
        }}
        
        .summary-card.total {{ border-left-color: #3b82f6; }}
        .summary-card.passed {{ border-left-color: #10b981; }}
        .summary-card.failed {{ border-left-color: #ef4444; }}
        .summary-card.duration {{ border-left-color: #8b5cf6; }}
        
        .summary-card h3 {{ 
            font-size: 2rem; 
            margin-bottom: 0.5rem; 
            font-weight: 700;
        }}
        
        .summary-card p {{ 
            color: #6b7280; 
            font-size: 0.9rem; 
            text-transform: uppercase; 
            font-weight: 600; 
            letter-spacing: 0.5px;
        }}
        
        .success-rate {{ 
            background: linear-gradient(90deg, #10b981, #059669); 
            color: white; 
            padding: 1rem; 
            border-radius: 8px; 
            text-align: center; 
            margin-bottom: 2rem;
            font-size: 1.2rem;
            font-weight: 600;
        }}
        
        .test-categories {{ margin-top: 2rem; }}
        
        .category-card {{ 
            background: white; 
            border: 1px solid #e5e7eb; 
            border-radius: 8px; 
            margin-bottom: 1.5rem; 
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        
        .category-header {{ 
            padding: 1.5rem; 
            background: #f9fafb; 
            border-bottom: 1px solid #e5e7eb;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        
        .category-header h3 {{ 
            font-size: 1.2rem; 
            margin: 0;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .category-status {{ 
            padding: 0.25rem 0.75rem; 
            border-radius: 20px; 
            font-size: 0.8rem; 
            font-weight: 600; 
            text-transform: uppercase;
        }}
        
        .status-pass {{ background: #dcfce7; color: #166534; }}
        .status-fail {{ background: #fee2e2; color: #dc2626; }}
        .status-mixed {{ background: #fef3c7; color: #92400e; }}
        
        .category-details {{ padding: 1.5rem; }}
        
        .detail-grid {{ 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); 
            gap: 1rem; 
            margin-bottom: 1rem;
        }}
        
        .detail-item {{ text-align: center; }}
        .detail-item .value {{ 
            font-size: 1.5rem; 
            font-weight: 700; 
            display: block;
        }}
        .detail-item .label {{ 
            font-size: 0.8rem; 
            color: #6b7280; 
            text-transform: uppercase;
        }}
        
        .test-list {{ margin-top: 1rem; }}
        .test-item {{ 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            padding: 0.5rem 0; 
            border-bottom: 1px solid #f3f4f6;
        }}
        .test-item:last-child {{ border-bottom: none; }}
        
        .error-section {{ 
            background: #fef2f2; 
            border: 1px solid #fecaca; 
            border-radius: 6px; 
            padding: 1rem; 
            margin-top: 1rem;
        }}
        
        .error-section h4 {{ 
            color: #dc2626; 
            margin-bottom: 0.5rem; 
            font-size: 0.9rem;
        }}
        
        .test-coverage {{ 
            background: #f0f9ff; 
            border: 1px solid #bae6fd; 
            border-radius: 6px; 
            padding: 1rem; 
            margin-top: 1rem;
        }}
        
        .coverage-grid {{ 
            display: grid; 
            gap: 0.5rem; 
            margin-top: 0.5rem;
        }}
        
        .coverage-item {{ 
            padding: 0.5rem; 
            background: white; 
            border-radius: 4px; 
            border-left: 3px solid #3b82f6;
            font-size: 0.9rem;
        }}
        
        .coverage-item strong {{ 
            color: #1e40af; 
            margin-right: 0.5rem;
        }}
        
        .execution-details {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 1.5rem;
            margin: 1.5rem 0;
        }}
        
        .detail-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1rem;
            margin-top: 1rem;
        }}
        
        .detail-item {{
            background: white;
            padding: 0.75rem 1rem;
            border-radius: 6px;
            border: 1px solid #e5e7eb;
            font-size: 0.9rem;
        }}
        
        .detail-item strong {{
            color: #1e40af;
            margin-right: 0.5rem;
        }}
        
        .error-content {{ 
            font-family: 'Monaco', 'Consolas', monospace; 
            font-size: 0.8rem; 
            background: white; 
            padding: 0.75rem; 
            border-radius: 4px; 
            border: 1px solid #f3f4f6;
            white-space: pre-wrap;
            max-height: 200px;
            overflow-y: auto;
        }}
        
        .footer {{ 
            background: #f9fafb; 
            padding: 2rem; 
            text-align: center; 
            border-top: 1px solid #e5e7eb;
            color: #6b7280;
        }}
        
        .badge {{ 
            display: inline-block; 
            padding: 0.25rem 0.5rem; 
            border-radius: 4px; 
            font-size: 0.75rem; 
            font-weight: 600;
        }}
        
        .badge-success {{ background: #dcfce7; color: #166534; }}
        .badge-error {{ background: #fee2e2; color: #dc2626; }}
        
        @media (max-width: 768px) {{
            body {{ padding: 1rem; }}
            .header {{ padding: 2rem 1rem; }}
            .header h1 {{ font-size: 2rem; }}
            .summary-grid {{ grid-template-columns: 1fr 1fr; }}
            .content {{ padding: 1rem; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🌊 FLUID Build Test Report</h1>
            <div class="subtitle">
                Session {summary['session_id']} • Generated {summary['end_time'][:19].replace('T', ' ')}
            </div>
        </div>
        
        <div class="content">
            <div class="summary-grid">
                <div class="summary-card total">
                    <h3>{summary['total_tests']}</h3>
                    <p>Total Categories</p>
                </div>
                <div class="summary-card passed">
                    <h3>{summary['total_passed']}</h3>
                    <p>Tests Passed</p>
                </div>
                <div class="summary-card failed">
                    <h3>{summary['total_failed']}</h3>
                    <p>Tests Failed</p>
                </div>
                <div class="summary-card duration">
                    <h3>{summary['total_duration']:.1f}s</h3>
                    <p>Total Duration</p>
                </div>
            </div>
            
            <div class="execution-details">
                <h3 style="color: #374151; margin-bottom: 1rem;">🔧 Execution Environment</h3>
                <div class="detail-grid">
                    <div class="detail-item">
                        <strong>Test Mode:</strong> {"Comprehensive" if self.include_all else "Core"} Test Suite
                    </div>
                    <div class="detail-item">
                        <strong>Python Version:</strong> {platform.python_version()}
                    </div>
                    <div class="detail-item">
                        <strong>Platform:</strong> {platform.system()} {platform.release()}
                    </div>
                    <div class="detail-item">
                        <strong>Working Directory:</strong> {os.getcwd()}
                    </div>
                    <div class="detail-item">
                        <strong>Start Time:</strong> {summary['start_time'][:19].replace('T', ' ')}
                    </div>
                    <div class="detail-item">
                        <strong>End Time:</strong> {summary['end_time'][:19].replace('T', ' ')}
                    </div>
                </div>
            </div>
            
            <div class="success-rate">
                🎯 Success Rate: {summary['success_rate']:.1f}% • Average Duration: {avg_duration:.1f}s per category
            </div>
            
            <div class="test-categories">
                <h2 style="margin-bottom: 1.5rem; color: #374151;">📋 Test Categories</h2>
"""

        # Generate detailed category information
        for category, result in summary["categories"].items():
            category_name = category.replace("_", " ").title()
            passed = result.get("passed", 0)
            failed = result.get("failed", 0)
            duration = result.get("duration", 0)

            # Determine status
            if failed == 0:
                status_class = "status-pass"
                status_text = "✅ PASSED"
            elif passed == 0:
                status_class = "status-fail"
                status_text = "❌ FAILED"
            else:
                status_class = "status-mixed"
                status_text = "⚠️ MIXED"

            # Get individual test details if available
            test_details = result.get("test_details", [])
            execution_summary = result.get("execution_summary", {})

            html_content += f"""
                <div class="category-card">
                    <div class="category-header">
                        <h3>{category_name}</h3>
                        <span class="category-status {status_class}">{status_text}</span>
                    </div>
                    <div class="category-details">
                        <div class="detail-grid">
                            <div class="detail-item">
                                <span class="value" style="color: #10b981;">{passed}</span>
                                <span class="label">Passed</span>
                            </div>
                            <div class="detail-item">
                                <span class="value" style="color: #ef4444;">{failed}</span>
                                <span class="label">Failed</span>
                            </div>
                            <div class="detail-item">
                                <span class="value" style="color: #8b5cf6;">{duration:.2f}s</span>
                                <span class="label">Duration</span>
                            </div>
                            <div class="detail-item">
                                <span class="value" style="color: #0891b2;">{execution_summary.get('success_rate', 0):.1f}%</span>
                                <span class="label">Success Rate</span>
                            </div>
                        </div>
"""

            # Add individual test details if available
            if test_details:
                html_content += f"""
                        <div class="test-list">
                            <h4 style="margin-bottom: 0.5rem; color: #374151;">Individual Tests ({len(test_details)} executed)</h4>
"""
                for test in test_details:  # Show ALL tests, not just first 10
                    status_badge = (
                        "badge-success" if test.get("status") == "PASS" else "badge-error"
                    )
                    test_name = test.get("name", "Unknown Test")
                    test_details_info = test.get("details", "")

                    # Extract more meaningful test information
                    if "Unknown" in test_name and test_details_info:
                        # Use details as test name if name is unknown
                        display_name = (
                            test_details_info.replace("✅ ", "")
                            .replace("❌ ", "")
                            .replace("⚠️ ", "")
                        )
                    else:
                        display_name = test_name

                    html_content += f"""
                            <div class="test-item">
                                <span title="{test_details_info}">{display_name}</span>
                                <span class="badge {status_badge}">{test.get('status', 'UNKNOWN')}</span>
                            </div>
"""
                html_content += "                        </div>\n"

            # Add comprehensive test coverage details
            html_content += """
                        <div class="test-coverage">
                            <h4 style="margin-bottom: 0.5rem; color: #374151;">🔍 Test Coverage Details</h4>
                            <div class="coverage-grid">
"""

            # Add category-specific coverage information
            coverage_info = self._get_test_coverage_info(category_name, result)
            for area, details in coverage_info.items():
                html_content += f"""
                                <div class="coverage-item">
                                    <strong>{area}:</strong> {details}
                                </div>
"""

            html_content += """
                            </div>
                        </div>
"""

            # Add error information if the test failed
            if result.get("error") or result.get("stderr"):
                error_msg = result.get("error", result.get("stderr", ""))
                html_content += f"""
                        <div class="error-section">
                            <h4>🚨 Error Details</h4>
                            <div class="error-content">{error_msg[:500]}{'...' if len(error_msg) > 500 else ''}</div>
                        </div>
"""

            html_content += """
                    </div>
                </div>
"""

        # Add footer
        html_content += f"""
            </div>
        </div>
        
        <div class="footer">
            <p>
                Generated by FLUID Build Testing Framework • 
                Session Output: <code>{summary['output_directory']}</code>
            </p>
            <p style="margin-top: 0.5rem; font-size: 0.8rem;">
                Report generated on {summary['end_time'][:19].replace('T', ' ')} • 
                Project: <code>{summary['project_root']}</code>
            </p>
        </div>
    </div>
</body>
</html>
"""

        report_file = self.output_dir / "reports" / "enhanced_test_report.html"
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(html_content)

    def _get_test_coverage_info(self, category_name: str, result: Dict[str, Any]) -> Dict[str, str]:
        """Get detailed test coverage information for a category"""
        coverage = {}

        # Category-specific coverage details
        if category_name.lower() == "basic_cli":
            coverage.update(
                {
                    "CLI Commands": "version, help, validate, plan, apply, providers",
                    "Core Functions": "Contract validation, plan generation, basic operations",
                    "Output Formats": "JSON, text, error handling",
                    "Environment": "Python version, module imports, CLI accessibility",
                }
            )
        elif category_name.lower() == "advanced":
            coverage.update(
                {
                    "Advanced Commands": "visualize-plan, contract-tests, scaffold operations",
                    "Complex Workflows": "Multi-step operations, pipeline generation",
                    "Provider Integration": "GCP, local, snowflake provider compatibility",
                    "Error Scenarios": "Invalid inputs, missing dependencies, edge cases",
                }
            )
        elif category_name.lower() == "validation":
            coverage.update(
                {
                    "Schema Validation": "Contract schema compliance, data type validation",
                    "Business Rules": "Domain-specific validation, constraint checking",
                    "Error Reporting": "Detailed error messages, validation failure details",
                    "File Formats": "YAML parsing, JSON validation, configuration checks",
                }
            )
        elif "complete_cli_validation" in category_name.lower():
            coverage.update(
                {
                    "End-to-End CLI": "Full command line interface validation",
                    "Integration Tests": "Multi-command workflows, state management",
                    "Provider Testing": "All supported providers, authentication, connections",
                    "Performance": "Command execution speed, resource usage",
                }
            )
        elif "complete_cli_audit" in category_name.lower():
            coverage.update(
                {
                    "CLI Identity": "Command resolution, entrypoints, module accessibility",
                    "Environment": "Python version, dependencies, virtual environment",
                    "Tooling": "External tools (jq, yq, dot, gcloud) availability",
                    "Core Operations": "validate, plan, apply, providers commands",
                }
            )
        elif "complete_system_diagnostics" in category_name.lower():
            coverage.update(
                {
                    "System Health": "Environment validation, dependency checks",
                    "Provider Status": "All provider availability and configuration",
                    "Tool Validation": "Required external tools and their versions",
                    "Performance": "System resource usage, execution benchmarks",
                }
            )
        elif "plugin_system" in category_name.lower():
            coverage.update(
                {
                    "Template System": "All built-in templates (starter, analytics, ML, ETL, streaming)",
                    "Provider Plugins": "Local, GCP, AWS, Snowflake provider loading",
                    "Extensions": "Project history, environment validator, AI assistant",
                    "Registry System": "Component discovery, registration, validation",
                }
            )
        elif category_name.lower() == "visualization":
            coverage.update(
                {
                    "Graph Generation": "DOT format, PNG output, HTML visualization",
                    "Plan Visualization": "Execution plan rendering, dependency graphs",
                    "Interactive Elements": "Clickable nodes, hover information, zoom controls",
                }
            )
        elif category_name.lower() == "documentation":
            coverage.update(
                {
                    "Docs Generation": "Markdown output, API documentation, usage examples",
                    "Format Support": "Multi-format output, template rendering",
                    "Content Validation": "Documentation completeness, link checking",
                }
            )
        else:
            # Generic coverage for other categories
            coverage.update(
                {
                    "Test Execution": f'{result.get("passed", 0)} passed, {result.get("failed", 0)} failed',
                    "Duration": f'{result.get("duration", 0):.2f} seconds',
                    "Success Rate": f'{result.get("execution_summary", {}).get("success_rate", 0):.1f}%',
                }
            )

        # Add common coverage metrics
        if result.get("test_details"):
            test_count = len(result["test_details"])
            passed_count = len([t for t in result["test_details"] if t.get("status") == "PASS"])
            coverage["Test Execution"] = f"{test_count} tests executed ({passed_count} passed)"

        if result.get("duration"):
            coverage["Performance"] = f'Completed in {result["duration"]:.2f}s'

        return coverage


def main():
    """Main function to run tests"""
    parser = argparse.ArgumentParser(description="FLUID Build Consolidated Test Runner")
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=[
            # Individual test categories (existing files)
            "basic_cli",
            "advanced",
            "validation",
            "planning",
            "apply",
            "visualization",
            "documentation",
            "market",
            "auth",
            # New CLI test categories
            "cli_viz_graph",
            "cli_core_commands",
            "cli_config_commands",
            # Plugin system tests
            "plugin_system",
            # Comprehensive test suites (existing files)
            "complete_cli_validation",
            "complete_cli_audit",
            "complete_system_diagnostics",
            "complete_test_matrix",
            "complete_orchestration",
            "cli_matrix",
            "edge_cases",
            "performance",
            "security",
            "compatibility",
            "cli_comprehensive",
            # Legacy choices for backward compatibility
            "basic",
            "system_diagnostics",
        ],
        help="Test categories to run (default: core test suite)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run ALL test categories including comprehensive suites (may take time)",
    )
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument(
        "--quick", action="store_true", help="Run tests in quick mode with minimal output"
    )

    args = parser.parse_args()

    try:
        project_root = find_project_root()

        output_dir = None
        if args.output_dir:
            output_dir = Path(args.output_dir)

        runner = ConsolidatedTestRunner(project_root, output_dir, quick_mode=args.quick)
        summary = runner.run_all_tests(args.categories, run_all=args.all)

        # Print final summary
        if RICH_AVAILABLE:
            console.print("\n")
            console.print("🌊 FLUID Build Test Results", style="bold cyan")
            console.print("=" * 50)
            console.print(f"Categories: {summary['total_tests']}")
            console.print(f"Passed: {summary['total_passed']}")
            console.print(f"Failed: {summary['total_failed']}")
            console.print(f"Success Rate: {summary['success_rate']:.1f}%")
            console.print(f"Duration: {summary['total_duration']:.1f}s")
            console.print("\n📁 Reports Generated:")
            console.print(f"   HTML: {runner.output_dir / 'reports' / 'enhanced_test_report.html'}")
            console.print(f"   JSON: {runner.output_dir / 'reports' / 'session_summary.json'}")
        else:
            print("\n=== FLUID Build Test Results ===")
            print(f"Categories: {summary['total_tests']}")
            print(f"Passed: {summary['total_passed']}")
            print(f"Failed: {summary['total_failed']}")
            print(f"Success Rate: {summary['success_rate']:.1f}%")
            print(f"Duration: {summary['total_duration']:.1f}s")

        # Exit with appropriate code
        if summary["total_failed"] > 0:
            print(f"\n❌ {summary['total_failed']} test categories failed!")
            sys.exit(1)
        else:
            print("\n✅ All tests passed!")
            sys.exit(0)

    except Exception as e:
        if RICH_AVAILABLE:
            console.print(f"[red]❌ Test execution failed: {e}[/red]")
        else:
            print(f"❌ Test execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
