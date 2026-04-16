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
Generate Build Test Report

Reads test results and coverage data, generates a simple markdown report
showing what was tested and what risk you're taking.

Usage:
    python generate_test_report.py [--profile alpha|beta|stable]
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml


def load_manifest():
    """Load build manifest"""
    manifest_path = Path(__file__).parent.parent / "fluid_build" / "build-manifest.yaml"
    with open(manifest_path) as f:
        return yaml.safe_load(f)


def load_coverage():
    """Load coverage data from pytest"""
    coverage_path = Path(__file__).parent.parent / "coverage.json"
    if not coverage_path.exists():
        return {"totals": {"percent_covered": 0}}

    with open(coverage_path) as f:
        return json.load(f)


def load_test_results():
    """Load test results from pytest"""
    results_path = Path(__file__).parent.parent / "test-results.json"
    if not results_path.exists():
        return {"tests": []}

    with open(results_path) as f:
        return json.load(f)


def analyze_provider_coverage(coverage_data):
    """Analyze coverage per provider"""
    providers = {}

    # Get coverage by file pattern
    files = coverage_data.get("files", {})

    for filepath, file_cov in files.items():
        # Extract provider from path like "providers/gcp/..."
        if "/providers/" in filepath:
            parts = filepath.split("/providers/")
            if len(parts) > 1:
                provider = parts[1].split("/")[0]
                if provider not in providers:
                    providers[provider] = {"lines_covered": 0, "lines_total": 0, "files": []}

                summary = file_cov.get("summary", {})
                providers[provider]["lines_covered"] += summary.get("covered_lines", 0)
                providers[provider]["lines_total"] += summary.get("num_statements", 0)
                providers[provider]["files"].append(filepath)

    # Calculate percentages
    for provider in providers:
        total = providers[provider]["lines_total"]
        if total > 0:
            covered = providers[provider]["lines_covered"]
            providers[provider]["coverage_pct"] = round((covered / total) * 100, 1)
        else:
            providers[provider]["coverage_pct"] = 0

    return providers


def analyze_command_tests(test_results):
    """Analyze tests by command"""
    commands = {}

    for test in test_results.get("tests", []):
        # Extract command from test path like "test_validate.py::test_..."
        test_file = test.get("nodeid", "").split("::")[0]
        if test_file.startswith("test_"):
            cmd = test_file.replace("test_", "").replace(".py", "")
            if cmd not in commands:
                commands[cmd] = {"passed": 0, "failed": 0, "skipped": 0}

            outcome = test.get("outcome", "failed")
            if outcome == "passed":
                commands[cmd]["passed"] += 1
            elif outcome == "failed":
                commands[cmd]["failed"] += 1
            elif outcome == "skipped":
                commands[cmd]["skipped"] += 1

    return commands


def generate_report(profile="alpha"):
    """Generate the test report"""

    manifest = load_manifest()
    coverage = load_coverage()
    test_results = load_test_results()

    build = manifest["builds"].get(profile, {})

    # Analyze data
    provider_cov = analyze_provider_coverage(coverage)
    command_tests = analyze_command_tests(test_results)

    # Overall stats
    overall_cov = coverage.get("totals", {}).get("percent_covered", 0)
    total_tests = len(test_results.get("tests", []))
    passed = sum(1 for t in test_results.get("tests", []) if t.get("outcome") == "passed")
    failed = sum(1 for t in test_results.get("tests", []) if t.get("outcome") == "failed")

    # Generate markdown report
    report = f"""# Build Test Report - {profile.upper()}

**Generated:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}  
**Profile:** {profile}  
**Description:** {build.get("description", "N/A")}

---

## Summary

| Metric | Value | Status |
|--------|-------|--------|
| Overall Coverage | {overall_cov:.1f}% | {'✅ Good' if overall_cov >= 70 else '⚠️ Needs Work' if overall_cov >= 30 else '❌ Low'} |
| Total Tests | {total_tests} | {'✅' if total_tests > 100 else '⚠️' if total_tests > 50 else '❌'} |
| Passing | {passed} | {'✅' if failed == 0 else '❌'} |
| Failing | {failed} | {'✅' if failed == 0 else '❌'} |

---

## Providers in This Build

| Provider | Coverage | Files | Test Status | Risk |
|----------|----------|-------|-------------|------|
"""

    for provider in build.get("providers", []):
        cov_data = provider_cov.get(provider, {})
        cov_pct = cov_data.get("coverage_pct", 0)
        file_count = len(cov_data.get("files", []))

        # Risk assessment
        if cov_pct >= 80:
            risk = "🟢 Low"
        elif cov_pct >= 50:
            risk = "🟡 Medium"
        elif cov_pct >= 30:
            risk = "🟠 High"
        else:
            risk = "🔴 Very High"

        report += f"| {provider} | {cov_pct:.1f}% | {file_count} | {'✅' if cov_pct > 0 else '❌'} | {risk} |\n"

    report += """
---

## Commands in This Build

| Command | Tests | Passed | Failed | Coverage |
|---------|-------|--------|--------|----------|
"""

    for cmd in build.get("commands", []):
        test_data = command_tests.get(cmd, {"passed": 0, "failed": 0, "skipped": 0})
        total_cmd_tests = test_data["passed"] + test_data["failed"] + test_data["skipped"]

        report += f"| {cmd} | {total_cmd_tests} | {test_data['passed']} | {test_data['failed']} | {'✅' if test_data['failed'] == 0 else '❌'} |\n"

    report += """
---

## Risk Assessment

### What You're Shipping:
"""

    # Count components
    num_providers = len(build.get("providers", []))
    num_commands = len(build.get("commands", []))

    report += f"""
- **{num_providers} providers**: {", ".join(build.get("providers", []))}
- **{num_commands} commands**: {", ".join(build.get("commands", []))}

### Coverage Analysis:
"""

    # Provider risk
    high_risk_providers = [
        p for p in build.get("providers", []) if provider_cov.get(p, {}).get("coverage_pct", 0) < 50
    ]

    if high_risk_providers:
        report += f"""
⚠️ **High-risk providers** (coverage < 50%): {", ".join(high_risk_providers)}
"""
    else:
        report += """
✅ All providers have acceptable coverage (≥50%)
"""

    # Test failures
    if failed > 0:
        report += f"""
❌ **{failed} failing tests** - Do not ship until fixed
"""
    else:
        report += """
✅ All tests passing
"""

    # Overall recommendation
    report += """

### Recommendation:
"""

    if failed > 0:
        report += """
**DO NOT SHIP** - Fix failing tests first
"""
    elif overall_cov < 30:
        report += """
**HIGH RISK** - Coverage below 30%. Add more tests or reduce scope.
"""
    elif overall_cov < 60:
        report += f"""
**MEDIUM RISK** - Coverage at {overall_cov:.1f}%. Good for {profile} but needs improvement.
"""
    else:
        report += f"""
**LOW RISK** - Coverage at {overall_cov:.1f}%. {profile.upper()} build is well tested.
"""

    if high_risk_providers and profile == "stable":
        report += f"""

⚠️ **Warning**: Stable build includes high-risk providers: {", ".join(high_risk_providers)}  
Consider moving these to beta or improving their test coverage.
"""

    report += """

---

## Next Steps

1. **Review the risk assessment above**
2. **Decide**: Ship as-is, add tests, or reduce scope
3. **Update build-manifest.yaml** if you want to change what's included
4. **Run tests again** after making changes

---

*This report is for decision-making only. It doesn't control what gets packaged.*  
*Edit build-manifest.yaml to control what goes in each build.*
"""

    return report


def main():
    """Main entry point"""
    profile = "alpha"
    if len(sys.argv) > 1 and sys.argv[1] in ["alpha", "beta", "stable"]:
        profile = sys.argv[1]

    report = generate_report(profile)

    # Write report
    output_path = Path(__file__).parent.parent / "build-test-report.md"
    with open(output_path, "w") as f:
        f.write(report)

    print(f"✅ Test report generated: {output_path}")
    print(f"📊 Profile: {profile}")
    print("\nReview build-test-report.md to assess risk before shipping.")


if __name__ == "__main__":
    main()
