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
Jenkins CI/CD Test Runner for FLUID Build CLI

This runner provides Jenkins-optimized test execution with:
- Category-based test selection (cli, runtime, all)
- Coverage reporting with thresholds
- JSON and JUnit XML output
- Enhanced error reporting
- Performance metrics

Usage:
    python tests/jenkins_runner.py --category cli --coverage --fail-under 70.0
    python tests/jenkins_runner.py --category runtime --json-report results.json
    python tests/jenkins_runner.py --category all --junit-xml junit.xml
"""

# Version tracking for debugging CI/CD issues
JENKINS_RUNNER_VERSION = "2.5.0"  # Added test discovery diagnostics
GIT_COMMIT_HINT = "261610a+"  # Update with git commit hash

import os
import sys
from pathlib import Path

print(f"🔍 Jenkins Runner v{JENKINS_RUNNER_VERSION} (commit: {GIT_COMMIT_HINT})")
print(f"📂 Script location: {__file__}")
print(f"📂 Working directory: {os.getcwd()}")
print(f"📂 Python executable: {sys.executable}")

# CRITICAL: Add paths BEFORE any other imports
PROJECT_ROOT = Path(__file__).parent.parent
TESTS_DIR = Path(__file__).parent
print(f"📂 PROJECT_ROOT: {PROJECT_ROOT}")
print(f"📂 TESTS_DIR: {TESTS_DIR}")

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TESTS_DIR))
print(f"🔧 sys.path (first 3): {sys.path[:3]}")

# Change to tests directory to ensure relative imports work
os.chdir(str(TESTS_DIR))
print(f"📂 Changed to: {os.getcwd()}")
print(f"📂 Files in tests dir: {list(Path('.').glob('*.py'))[:5]}")

# Now safe to import other modules
import argparse
import json
import shutil
import time
import unittest
from datetime import datetime
from typing import Any, Dict, Tuple

print("🔍 Attempting to import test_runner...")
from test_runner import discover_all_tests, discover_cli_tests, discover_runtime_tests

print("✅ Successfully imported test_runner")


class JenkinsTestResult(unittest.TextTestResult):
    """Enhanced test result with timing and metadata collection."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_timings = {}
        self.test_metadata = {}

    def startTest(self, test):
        super().startTest(test)
        self.test_timings[str(test)] = time.time()

    def stopTest(self, test):
        super().stopTest(test)
        test_id = str(test)
        if test_id in self.test_timings:
            duration = time.time() - self.test_timings[test_id]
            self.test_metadata[test_id] = {
                "duration": duration,
                "status": (
                    "passed"
                    if test_id not in [str(f[0]) for f in self.failures + self.errors]
                    else "failed"
                ),
            }


def run_tests_with_coverage(
    suite: unittest.TestSuite, verbose: int = 2, fail_under: float = 0.0
) -> Tuple[JenkinsTestResult, float]:
    """
    Run tests with coverage reporting.

    Args:
        suite: Test suite to run
        verbose: Verbosity level (0-2)
        fail_under: Minimum coverage percentage required

    Returns:
        Tuple of (test_result, coverage_percentage)
    """
    try:
        import coverage

        # Initialize coverage
        cov = coverage.Coverage(
            source=["fluid_build"],
            omit=[
                "*/tests/*",
                "*/test_*.py",
                "*/__pycache__/*",
                "*/venv/*",
                "*/.venv/*",
                "*/site-packages/*",
            ],
        )

        cov.start()

        # Run tests with enhanced result class
        runner = unittest.TextTestRunner(verbosity=verbose, resultclass=JenkinsTestResult)
        result = runner.run(suite)

        cov.stop()
        cov.save()

        # Generate reports
        print("\n" + "=" * 70)
        print("Coverage Report")
        print("=" * 70)
        coverage_pct = cov.report()

        # Generate HTML coverage report
        html_dir = PROJECT_ROOT / "htmlcov"
        html_dir.mkdir(parents=True, exist_ok=True)
        cov.html_report(directory=str(html_dir))
        print(f"\n📊 HTML coverage report: {html_dir}/index.html")

        # Generate XML coverage report for Jenkins
        xml_file = PROJECT_ROOT / "coverage.xml"
        cov.xml_report(outfile=str(xml_file))
        print(f"📊 XML coverage report: {xml_file}")

        # Check coverage threshold
        if fail_under > 0 and coverage_pct < fail_under:
            print(f"\n❌ Coverage {coverage_pct:.2f}% is below threshold {fail_under:.2f}%")
        else:
            print(f"\n✅ Coverage {coverage_pct:.2f}% meets threshold")

        return result, coverage_pct

    except ImportError:
        print("⚠️  Coverage module not available. Install with: pip install coverage")
        print("Running tests without coverage...")

        runner = unittest.TextTestRunner(verbosity=verbose, resultclass=JenkinsTestResult)
        result = runner.run(suite)
        return result, 0.0


def generate_json_report(
    result: JenkinsTestResult,
    coverage_pct: float,
    category: str,
    duration: float,
    output_file: Path,
) -> Dict[str, Any]:
    """Generate JSON test report for Jenkins."""

    report = {
        "timestamp": datetime.now().isoformat(),
        "category": category,
        "summary": {
            "total_tests": result.testsRun,
            "passed": result.testsRun - len(result.failures) - len(result.errors),
            "failed": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "duration_seconds": round(duration, 2),
        },
        "coverage": {
            "percentage": round(coverage_pct, 2),
            "html_report": "htmlcov/index.html",
            "xml_report": "coverage.xml",
        },
        "success": result.wasSuccessful(),
        "failures": [
            {"test": str(test), "traceback": traceback} for test, traceback in result.failures
        ],
        "errors": [
            {"test": str(test), "traceback": traceback} for test, traceback in result.errors
        ],
        "test_details": result.test_metadata if hasattr(result, "test_metadata") else {},
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, indent=2))
    print(f"\n📄 JSON report: {output_file}")

    return report


def generate_junit_xml(
    result: JenkinsTestResult, category: str, duration: float, output_file: Path
) -> None:
    """Generate JUnit XML report for Jenkins."""

    try:
        import xml.etree.ElementTree as ET

        # Create root testsuite element
        testsuite = ET.Element(
            "testsuite",
            {
                "name": f"FLUID CLI {category} Tests",
                "tests": str(result.testsRun),
                "failures": str(len(result.failures)),
                "errors": str(len(result.errors)),
                "skipped": str(len(result.skipped)),
                "time": str(round(duration, 3)),
                "timestamp": datetime.now().isoformat(),
            },
        )

        # Add test cases
        for test_id, metadata in getattr(result, "test_metadata", {}).items():
            testcase = ET.SubElement(
                testsuite,
                "testcase",
                {
                    "name": test_id.split(".")[-1],
                    "classname": ".".join(test_id.split(".")[:-1]),
                    "time": str(round(metadata["duration"], 3)),
                },
            )

            # Add failure information if applicable
            for test, traceback in result.failures:
                if str(test) == test_id:
                    failure = ET.SubElement(
                        testcase, "failure", {"type": "AssertionError", "message": "Test failed"}
                    )
                    failure.text = traceback

            # Add error information if applicable
            for test, traceback in result.errors:
                if str(test) == test_id:
                    error = ET.SubElement(
                        testcase, "error", {"type": "Exception", "message": "Test error"}
                    )
                    error.text = traceback

        # Write to file
        tree = ET.ElementTree(testsuite)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        tree.write(str(output_file), encoding="utf-8", xml_declaration=True)
        print(f"\n📄 JUnit XML report: {output_file}")

    except ImportError:
        print("⚠️  xml.etree module not available. Skipping JUnit XML generation.")


def create_ai_log_archive(
    result: JenkinsTestResult,
    coverage_pct: float,
    category: str,
    duration: float,
    log_dir: str = "test-results",
) -> Path:
    """
    Create AI-readable timestamped log archive.

    Structure:
        test-results/
        └── 2026-01-07_18-30-45/
            ├── environment.json
            ├── test_results.json
            ├── TEST_SUMMARY.md
            ├── coverage/
            │   ├── coverage.xml
            │   └── html/
            ├── failures/
            │   └── test_name.json
            └── logs/
                └── test_name.json
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    archive_dir = PROJECT_ROOT / log_dir / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 1. Environment metadata
    env_data = {
        "timestamp": timestamp,
        "category": category,
        "python_version": sys.version,
        "platform": sys.platform,
        "cli_version": "2.0.0",  # TODO: Read from package
        "test_framework": "unittest/pytest",
        "coverage_tool": "coverage.py",
    }
    (archive_dir / "environment.json").write_text(json.dumps(env_data, indent=2))

    # 2. Test results JSON (same as existing report)
    test_data = {
        "timestamp": timestamp,
        "category": category,
        "summary": {
            "total_tests": result.testsRun,
            "passed": result.testsRun - len(result.failures) - len(result.errors),
            "failed": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "duration_seconds": round(duration, 2),
        },
        "coverage": {
            "percentage": round(coverage_pct, 2),
            "html_report": "coverage/html/index.html",
            "xml_report": "coverage/coverage.xml",
        },
        "success": result.wasSuccessful(),
    }
    (archive_dir / "test_results.json").write_text(json.dumps(test_data, indent=2))

    # 3. TEST_SUMMARY.md (AI-readable markdown)
    summary_md = f"""# CLI Test Results - {timestamp}

## Summary

- **Category:** {category}
- **Status:** {'✅ PASSED' if result.wasSuccessful() else '❌ FAILED'}
- **Total Tests:** {result.testsRun}
- **Passed:** {result.testsRun - len(result.failures) - len(result.errors)}
- **Failed:** {len(result.failures)}
- **Errors:** {len(result.errors)}
- **Skipped:** {len(result.skipped)}
- **Duration:** {duration:.2f}s
- **Coverage:** {coverage_pct:.2f}%

## Environment

- **Python:** {sys.version.split()[0]}
- **Platform:** {sys.platform}
- **CLI Version:** 2.0.0

## Test Details

### Passed Tests ({result.testsRun - len(result.failures) - len(result.errors)})

"""

    # Add passed tests
    for test_id, metadata in getattr(result, "test_metadata", {}).items():
        if metadata["status"] == "passed":
            summary_md += f"- ✅ `{test_id}` ({metadata['duration']:.3f}s)\n"

    if result.failures:
        summary_md += f"\n### Failed Tests ({len(result.failures)})\n\n"
        for test, traceback in result.failures:
            summary_md += f"- ❌ `{test}`\n"
            summary_md += f"  ```\n  {traceback[:200]}...\n  ```\n"

    if result.errors:
        summary_md += f"\n### Errors ({len(result.errors)})\n\n"
        for test, traceback in result.errors:
            summary_md += f"- ⚠️  `{test}`\n"
            summary_md += f"  ```\n  {traceback[:200]}...\n  ```\n"

    (archive_dir / "TEST_SUMMARY.md").write_text(summary_md)

    # 4. Individual test logs
    logs_dir = archive_dir / "logs"
    logs_dir.mkdir(exist_ok=True)

    for test_id, metadata in getattr(result, "test_metadata", {}).items():
        test_log = {
            "test_id": test_id,
            "status": metadata["status"],
            "duration": metadata["duration"],
            "timestamp": timestamp,
        }

        # Add failure/error details
        for test, traceback in result.failures:
            if str(test) == test_id:
                test_log["failure"] = traceback

        for test, traceback in result.errors:
            if str(test) == test_id:
                test_log["error"] = traceback

        # Safe filename
        safe_name = test_id.replace(" ", "_").replace("(", "_").replace(")", "_")
        log_file = logs_dir / f"{safe_name}.json"
        log_file.write_text(json.dumps(test_log, indent=2))

    # 5. Failure details (separate directory)
    if result.failures or result.errors:
        failures_dir = archive_dir / "failures"
        failures_dir.mkdir(exist_ok=True)

        for test, traceback in result.failures:
            safe_name = str(test).replace(" ", "_").replace("(", "_").replace(")", "_")
            failure_file = failures_dir / f"{safe_name}.json"
            failure_file.write_text(
                json.dumps(
                    {
                        "test": str(test),
                        "type": "failure",
                        "traceback": traceback,
                        "timestamp": timestamp,
                    },
                    indent=2,
                )
            )

        for test, traceback in result.errors:
            safe_name = str(test).replace(" ", "_").replace("(", "_").replace(")", "_")
            error_file = failures_dir / f"{safe_name}.json"
            error_file.write_text(
                json.dumps(
                    {
                        "test": str(test),
                        "type": "error",
                        "traceback": traceback,
                        "timestamp": timestamp,
                    },
                    indent=2,
                )
            )

    # 6. Copy coverage reports
    coverage_dir = archive_dir / "coverage"
    coverage_dir.mkdir(exist_ok=True)

    # Copy coverage.xml if exists
    if (PROJECT_ROOT / "coverage.xml").exists():
        shutil.copy2(PROJECT_ROOT / "coverage.xml", coverage_dir / "coverage.xml")

    # Copy htmlcov directory if exists
    if (PROJECT_ROOT / "htmlcov").exists():
        html_dest = coverage_dir / "html"
        if html_dest.exists():
            shutil.rmtree(html_dest)
        shutil.copytree(PROJECT_ROOT / "htmlcov", html_dest)

    # 7. Create artifacts directory (empty, for future use)
    (archive_dir / "artifacts").mkdir(exist_ok=True)

    print(f"\n📁 AI-readable logs archived to: {archive_dir}")
    print("   - Environment: environment.json")
    print("   - Results: test_results.json")
    print("   - Summary: TEST_SUMMARY.md")
    print(f"   - Individual logs: logs/ ({len(getattr(result, 'test_metadata', {}))} files)")
    if result.failures or result.errors:
        print(f"   - Failures: failures/ ({len(result.failures) + len(result.errors)} files)")
    print("   - Coverage: coverage/")

    return archive_dir


def print_summary(result: JenkinsTestResult, coverage_pct: float, duration: float) -> None:
    """Print test execution summary."""

    print("\n" + "=" * 70)
    print("Test Execution Summary")
    print("=" * 70)
    print(f"Total Tests:     {result.testsRun}")
    print(f"✅ Passed:       {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"❌ Failed:       {len(result.failures)}")
    print(f"⚠️  Errors:       {len(result.errors)}")
    print(f"⏭️  Skipped:      {len(result.skipped)}")
    print(f"⏱️  Duration:     {duration:.2f}s")
    print(f"📊 Coverage:     {coverage_pct:.2f}%")
    print("=" * 70)

    if result.wasSuccessful():
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed!")

        if result.failures:
            print(f"\n❌ Failures ({len(result.failures)}):")
            for test, _ in result.failures:
                print(f"  - {test}")

        if result.errors:
            print(f"\n⚠️  Errors ({len(result.errors)}):")
            for test, _ in result.errors:
                print(f"  - {test}")


def main():
    """Main entry point for Jenkins test runner."""

    parser = argparse.ArgumentParser(
        description="Jenkins CI/CD test runner for FLUID Build CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run all CLI tests with coverage
  python tests/jenkins_runner.py --category cli --coverage
  
  # Run runtime tests with 60% threshold
  python tests/jenkins_runner.py --category runtime --fail-under 60.0
  
  # Run all tests with JSON and JUnit output
  python tests/jenkins_runner.py --category all --json-report report.json --junit-xml junit.xml
  
  # Quick smoke test
  python tests/jenkins_runner.py --category cli --no-coverage --verbose 1
        """,
    )

    # Test selection
    parser.add_argument(
        "--category",
        choices=["cli", "runtime", "all"],
        default="all",
        help="Test category to run (default: all)",
    )

    # Coverage options
    parser.add_argument(
        "--coverage",
        action="store_true",
        default=True,
        help="Run tests with coverage (default: True)",
    )
    parser.add_argument(
        "--no-coverage", action="store_false", dest="coverage", help="Disable coverage reporting"
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=0.0,
        help="Minimum coverage percentage required (default: 0.0)",
    )

    # Output options
    parser.add_argument("--json-report", type=str, help="Path for JSON test report output")
    parser.add_argument("--junit-xml", type=str, help="Path for JUnit XML test report output")
    parser.add_argument(
        "--verbose",
        type=int,
        choices=[0, 1, 2],
        default=2,
        help="Verbosity level (0=quiet, 1=normal, 2=verbose, default: 2)",
    )

    # AI logging options
    parser.add_argument(
        "--ai-logs",
        action="store_true",
        default=False,
        help="Generate AI-readable timestamped log archive (for CI/CD)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default="test-results",
        help="Base directory for AI log archives (default: test-results)",
    )

    args = parser.parse_args()

    # Print configuration
    print("=" * 70)
    print("FLUID Build CLI - Jenkins Test Runner")
    print("=" * 70)
    print(f"Category:        {args.category}")
    print(f"Coverage:        {'Enabled' if args.coverage else 'Disabled'}")
    print(f"Coverage Threshold: {args.fail_under:.1f}%")
    print(f"Verbosity:       {args.verbose}")
    print("=" * 70 + "\n")

    # Discover tests based on category
    start_time = time.time()

    if args.category == "cli":
        print("🔍 Discovering CLI tests...")
        suite = discover_cli_tests()
        report_name = "CLI Tests"
    elif args.category == "runtime":
        print("🔍 Discovering runtime tests...")
        suite = discover_runtime_tests()
        report_name = "Runtime Tests"
    else:
        print("🔍 Discovering all tests...")
        suite = discover_all_tests()
        report_name = "All Tests"

    # Count tests
    test_count = suite.countTestCases()
    print(f"📊 Discovered {test_count} tests")

    if test_count == 0:
        print("❌ ERROR: No tests were discovered!")
        print(f"📂 Current directory: {os.getcwd()}")
        print("📂 Looking for tests in:")
        if args.category == "cli":
            cli_dir = Path("cli")
            print(f"   - cli/ directory: {cli_dir.exists()}")
            if cli_dir.exists():
                test_files = list(cli_dir.glob("test_*.py"))
                print(
                    f"   - Found {len(test_files)} test_*.py files: {[f.name for f in test_files[:5]]}"
                )
        elif args.category == "runtime":
            runtime_dir = Path("runtimes")
            print(f"   - runtimes/ directory: {runtime_dir.exists()}")
            if runtime_dir.exists():
                test_files = list(runtime_dir.glob("test_*.py"))
                print(
                    f"   - Found {len(test_files)} test_*.py files: {[f.name for f in test_files[:5]]}"
                )
        print("\n⚠️  Continuing anyway to generate reports...")

    print(f"Running {report_name}...\n")

    # Run tests (handle empty suite gracefully)
    if args.coverage and test_count > 0:
        result, coverage_pct = run_tests_with_coverage(
            suite, verbose=args.verbose, fail_under=args.fail_under
        )
    elif test_count > 0:
        runner = unittest.TextTestRunner(verbosity=args.verbose, resultclass=JenkinsTestResult)
        result = runner.run(suite)
        coverage_pct = 0.0
    else:
        # No tests found - create empty result
        print("⚠️  No tests to run - creating empty result")
        result = JenkinsTestResult(sys.stdout, False, args.verbose)
        result.testsRun = 0
        coverage_pct = 0.0

    duration = time.time() - start_time

    # Generate reports
    if args.json_report:
        generate_json_report(result, coverage_pct, args.category, duration, Path(args.json_report))

    if args.junit_xml:
        generate_junit_xml(result, args.category, duration, Path(args.junit_xml))

    # Print summary
    print_summary(result, coverage_pct, duration)

    # Create AI-readable log archive if requested
    if args.ai_logs:
        create_ai_log_archive(result, coverage_pct, args.category, duration, args.log_dir)
    else:
        print("\nℹ️  Tip: Run with --ai-logs to create timestamped archive for AI analysis")

    # Determine exit code
    exit_code = 0

    if not result.wasSuccessful():
        exit_code = 1
        print("\n❌ Tests failed")
    elif args.fail_under > 0 and coverage_pct < args.fail_under:
        exit_code = 1
        print(f"\n❌ Coverage {coverage_pct:.2f}% below threshold {args.fail_under:.2f}%")
    else:
        print("\n✅ All checks passed")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
