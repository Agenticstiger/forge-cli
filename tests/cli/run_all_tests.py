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
FLUID Build - Complete CLI Test Suite Runner

Runs comprehensive test coverage for all CLI commands and functionality.
This script orchestrates testing of:
- Visualization commands (viz-graph)
- Core commands (validate, plan, apply)
- Configuration commands (config, version, init)
- Integration and error handling scenarios
"""

import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import all test modules
try:
    from tests.cli.test_config_commands import run_tests as run_config_tests
    from tests.cli.test_core_commands import run_tests as run_core_tests
    from tests.cli.test_viz_graph import run_tests as run_viz_tests
    from tests.cli.test_viz_graph_integration import run_tests as run_viz_integration_tests
except ImportError as e:
    print(f"Warning: Could not import all test modules: {e}")
    print("Falling back to individual test discovery...")


def discover_and_run_tests() -> Dict[str, Any]:
    """Discover and run all CLI tests using unittest discovery."""
    results = {}

    # Test discovery patterns
    test_patterns = [
        "test_viz_graph.py",
        "test_viz_graph_integration.py",
        "test_core_commands.py",
        "test_config_commands.py",
    ]

    cli_test_dir = PROJECT_ROOT / "tests" / "cli"

    for pattern in test_patterns:
        test_file = cli_test_dir / pattern
        if not test_file.exists():
            print(f"⚠️  Test file not found: {pattern}")
            continue

        print(f"\n{'='*60}")
        print(f"Running tests from: {pattern}")
        print(f"{'='*60}")

        # Load and run tests from the specific file
        loader = unittest.TestLoader()
        try:
            # Import the module dynamically
            module_name = pattern[:-3]  # Remove .py
            spec = __import__(f"tests.cli.{module_name}", fromlist=[""])

            # Load tests from module
            suite = loader.loadTestsFromModule(spec)

            # Run tests
            runner = unittest.TextTestRunner(verbosity=2, buffer=True, stream=sys.stdout)

            start_time = time.time()
            result = runner.run(suite)
            end_time = time.time()

            # Store results
            results[pattern] = {
                "tests_run": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "skipped": len(getattr(result, "skipped", [])),
                "success": len(result.failures) == 0 and len(result.errors) == 0,
                "duration": end_time - start_time,
                "failure_details": [(str(test), traceback) for test, traceback in result.failures],
                "error_details": [(str(test), traceback) for test, traceback in result.errors],
            }

        except Exception as e:
            print(f"❌ Error running tests from {pattern}: {e}")
            results[pattern] = {
                "tests_run": 0,
                "failures": 0,
                "errors": 1,
                "skipped": 0,
                "success": False,
                "duration": 0,
                "failure_details": [],
                "error_details": [("Module Import", str(e))],
            }

    return results


def run_specific_test_modules() -> Dict[str, Any]:
    """Run tests using specific test module functions."""
    results = {}

    test_modules = [
        ("Visualization Tests", run_viz_tests),
        ("Visualization Integration Tests", run_viz_integration_tests),
        ("Core Commands Tests", run_core_tests),
        ("Configuration Commands Tests", run_config_tests),
    ]

    for module_name, test_function in test_modules:
        print(f"\n{'='*60}")
        print(f"Running: {module_name}")
        print(f"{'='*60}")

        try:
            start_time = time.time()
            exit_code = test_function()
            end_time = time.time()

            results[module_name] = {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "duration": end_time - start_time,
            }

            if exit_code == 0:
                print(f"✅ {module_name} completed successfully")
            else:
                print(f"❌ {module_name} failed with exit code {exit_code}")

        except Exception as e:
            print(f"❌ Error running {module_name}: {e}")
            results[module_name] = {
                "success": False,
                "exit_code": 1,
                "duration": 0,
                "error": str(e),
            }

    return results


def print_summary(results: Dict[str, Any]) -> bool:
    """Print comprehensive test summary."""
    print(f"\n{'='*80}")
    print("FLUID Build CLI Test Suite - Comprehensive Summary")
    print(f"{'='*80}")

    total_tests = 0
    total_failures = 0
    total_errors = 0
    total_skipped = 0
    total_duration = 0
    successful_modules = 0

    # Calculate totals
    for module_name, result in results.items():
        if isinstance(result, dict):
            if "tests_run" in result:
                # Detailed unittest results
                total_tests += result["tests_run"]
                total_failures += result["failures"]
                total_errors += result["errors"]
                total_skipped += result["skipped"]
                total_duration += result["duration"]

                if result["success"]:
                    successful_modules += 1

            elif "exit_code" in result:
                # Simple exit code results
                total_duration += result["duration"]
                if result["success"]:
                    successful_modules += 1

    # Print overall statistics
    print(f"Test Modules Run: {len(results)}")
    print(f"Successful Modules: {successful_modules}")
    print(f"Failed Modules: {len(results) - successful_modules}")

    if total_tests > 0:
        print("\nDetailed Test Statistics:")
        print(f"Total Tests Run: {total_tests}")
        print(f"Passed: {total_tests - total_failures - total_errors}")
        print(f"Failed: {total_failures}")
        print(f"Errors: {total_errors}")
        print(f"Skipped: {total_skipped}")
        print(
            f"Success Rate: {((total_tests - total_failures - total_errors) / total_tests * 100):.1f}%"
        )

    print(f"\nTotal Duration: {total_duration:.2f} seconds")

    # Print module-by-module results
    print(f"\n{'Module Results:':<40} {'Status':<10} {'Details'}")
    print(f"{'-'*70}")

    for module_name, result in results.items():
        if isinstance(result, dict):
            if "tests_run" in result:
                # Detailed results
                status = "✅ PASS" if result["success"] else "❌ FAIL"
                details = f"{result['tests_run']} tests, {result['failures']} failures, {result['errors']} errors"
            elif "exit_code" in result:
                # Simple results
                status = "✅ PASS" if result["success"] else "❌ FAIL"
                details = f"Exit code: {result['exit_code']}"
            else:
                status = "❓ UNKNOWN"
                details = "No result data"
        else:
            status = "❓ UNKNOWN"
            details = str(result)

        print(f"{module_name:<40} {status:<10} {details}")

    # Print failure details if any
    if total_failures > 0 or total_errors > 0:
        print(f"\n{'Failure and Error Details:'}")
        print(f"{'-'*70}")

        for module_name, result in results.items():
            if isinstance(result, dict) and (
                "failure_details" in result or "error_details" in result
            ):
                if result.get("failure_details"):
                    print(f"\n{module_name} - Failures:")
                    for test_name, traceback in result["failure_details"]:
                        print(f"  ❌ {test_name}")
                        # Print first few lines of traceback
                        lines = traceback.split("\n")[:3]
                        for line in lines:
                            if line.strip():
                                print(f"     {line}")

                if result.get("error_details"):
                    print(f"\n{module_name} - Errors:")
                    for test_name, traceback in result["error_details"]:
                        print(f"  ❌ {test_name}")
                        lines = traceback.split("\n")[:3]
                        for line in lines:
                            if line.strip():
                                print(f"     {line}")

    # Overall result
    overall_success = (
        successful_modules == len(results) and total_failures == 0 and total_errors == 0
    )

    print(f"\n{'='*80}")
    if overall_success:
        print("🎉 ALL TESTS PASSED! CLI Test Suite Complete")
        print(f"   Total: {total_tests} tests across {len(results)} modules")
        print(f"   Duration: {total_duration:.2f} seconds")
        print("   Coverage: Visualization, Core Commands, Configuration, Integration")
    else:
        print("❌ SOME TESTS FAILED")
        print(f"   Total Issues: {total_failures + total_errors}")
        print(f"   Failed Modules: {len(results) - successful_modules}/{len(results)}")

        if total_tests > 0:
            success_rate = (total_tests - total_failures - total_errors) / total_tests * 100
            print(f"   Success Rate: {success_rate:.1f}%")

    print(f"{'='*80}")

    return overall_success


def main():
    """Main test runner function."""
    print("FLUID Build - Comprehensive CLI Test Suite")
    print("Testing all CLI commands and functionality...\n")

    # Try to run with specific test functions first
    print("Attempting to run tests with specific test modules...")
    try:
        results = run_specific_test_modules()
        success = print_summary(results)
        return 0 if success else 1

    except Exception as e:
        print(f"Error with specific test modules: {e}")
        print("Falling back to test discovery...")

    # Fallback to test discovery
    try:
        results = discover_and_run_tests()
        success = print_summary(results)
        return 0 if success else 1

    except Exception as e:
        print(f"❌ Error running test suite: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
