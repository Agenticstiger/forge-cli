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
Generate comprehensive build report for AI/human analysis.

Outputs:
- JSON: Structured data for AI analysis
- Markdown: Human-readable report
- Logs: Consolidated test failures and warnings
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml


def load_json_safe(path: Path) -> Dict[str, Any]:
    """Load JSON file, return empty dict if not found."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def generate_build_report(
    features_path: Path,
    coverage_path: Path,
    test_report_path: Path,
    output_dir: Path,
    build_info: Dict[str, str],
) -> Dict[str, Any]:
    """Generate comprehensive build report."""

    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    features = yaml.safe_load(open(features_path)) if features_path.exists() else {}
    coverage = load_json_safe(coverage_path)
    test_report = load_json_safe(test_report_path)

    # Build comprehensive report
    report = {
        "build": {
            "number": build_info.get("BUILD_NUMBER", "unknown"),
            "tag": build_info.get("BUILD_TAG", "unknown"),
            "profile": build_info.get("BUILD_PROFILE", "unknown"),
            "branch": build_info.get("GIT_BRANCH", "unknown"),
            "commit": build_info.get("GIT_COMMIT", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "url": build_info.get("BUILD_URL", ""),
        },
        "test_results": {
            "summary": test_report.get("summary", {}),
            "duration": test_report.get("duration", 0),
            "failed_tests": [],
            "warnings": [],
        },
        "coverage": {
            "overall": coverage.get("totals", {}).get("percent_covered", 0),
            "by_provider": {},
            "uncovered_files": [],
        },
        "providers": {},
        "quality_status": {},
        "recommendations": [],
        "next_steps": [],
    }

    # Extract failed tests with full details
    for test in test_report.get("tests", []):
        if test.get("outcome") == "failed":
            report["test_results"]["failed_tests"].append(
                {
                    "name": test.get("nodeid", ""),
                    "error": test.get("call", {}).get("longrepr", ""),
                    "duration": test.get("call", {}).get("duration", 0),
                }
            )

    # Extract warnings
    for warning in test_report.get("warnings", []):
        report["test_results"]["warnings"].append(
            {
                "message": warning.get("message", ""),
                "filename": warning.get("filename", ""),
            }
        )

    # Analyze provider coverage
    for provider_name, provider_data in features.get("providers", {}).items():
        provider_cov = calculate_provider_coverage(coverage, provider_name)

        report["providers"][provider_name] = {
            "status": provider_data.get("status", "unknown"),
            "coverage": provider_cov["coverage"],
            "code_size": provider_data.get("code_size", 0),
            "blockers": provider_data.get("blockers", []),
            "ready_for_promotion": assess_promotion_readiness(provider_data, provider_cov),
        }

        report["coverage"]["by_provider"][provider_name] = provider_cov["coverage"]

    # Find files with low coverage
    for file_path, file_data in coverage.get("files", {}).items():
        pct = file_data["summary"]["percent_covered"]
        if pct < 50 and "fluid_build" in file_path:
            report["coverage"]["uncovered_files"].append(
                {
                    "file": file_path,
                    "coverage": pct,
                    "missing_lines": file_data["summary"]["missing_lines"],
                }
            )

    # Generate recommendations
    report["recommendations"] = generate_recommendations(report)
    report["next_steps"] = generate_next_steps(report)

    # Save JSON report (for AI consumption)
    json_path = output_dir / "build-report.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    # Save Markdown report (for human reading)
    md_path = output_dir / "build-report.md"
    with open(md_path, "w") as f:
        f.write(generate_markdown_report(report))

    # Save failure details (for quick debugging)
    if report["test_results"]["failed_tests"]:
        failures_path = output_dir / "test-failures.txt"
        with open(failures_path, "w") as f:
            f.write("# Test Failures\n\n")
            for test in report["test_results"]["failed_tests"]:
                f.write(f"## {test['name']}\n\n")
                f.write(f"```\n{test['error']}\n```\n\n")

    print("✅ Build report generated:")
    print(f"   JSON: {json_path}")
    print(f"   Markdown: {md_path}")
    if report["test_results"]["failed_tests"]:
        print(f"   Failures: {failures_path}")

    return report


def calculate_provider_coverage(coverage_data: Dict, provider_name: str) -> Dict:
    """Calculate coverage for a specific provider."""
    provider_pattern = f"fluid_build/providers/{provider_name}/"
    total_lines = 0
    covered_lines = 0

    for file_path, file_data in coverage_data.get("files", {}).items():
        if provider_pattern in file_path:
            summary = file_data["summary"]
            total_lines += summary["covered_lines"] + summary["missing_lines"]
            covered_lines += summary["covered_lines"]

    coverage = (covered_lines / total_lines * 100) if total_lines > 0 else 0
    return {
        "coverage": round(coverage, 1),
        "files": len([f for f in coverage_data.get("files", {}) if provider_pattern in f]),
    }


def assess_promotion_readiness(provider_data: Dict, coverage_info: Dict) -> Dict:
    """Assess if a provider is ready for promotion."""
    status = provider_data.get("status", "alpha")
    coverage = coverage_info["coverage"]
    blockers = provider_data.get("blockers", [])

    if status == "alpha":
        ready = coverage >= 60 and len(blockers) == 0
        next_level = "beta" if ready else "alpha"
        reason = (
            "Coverage >= 60%, no blockers"
            if ready
            else f"Need {60 - coverage:.1f}% more coverage or resolve blockers"
        )
    elif status == "beta":
        ready = coverage >= 80 and len(blockers) == 0
        next_level = "stable" if ready else "beta"
        reason = (
            "Coverage >= 80%, no blockers"
            if ready
            else f"Need {80 - coverage:.1f}% more coverage or resolve blockers"
        )
    else:
        ready = True
        next_level = "stable"
        reason = "Already at stable"

    return {
        "ready": ready,
        "next_level": next_level,
        "reason": reason,
        "blockers_count": len(blockers),
    }


def generate_recommendations(report: Dict) -> List[str]:
    """Generate actionable recommendations based on report data."""
    recommendations = []

    # Failed tests
    failed_count = len(report["test_results"]["failed_tests"])
    if failed_count > 0:
        recommendations.append(f"🔴 Fix {failed_count} failing test(s) before promotion")

    # Low coverage providers
    for provider, data in report["providers"].items():
        if data["coverage"] < 60:
            recommendations.append(
                f"🟡 Increase {provider} coverage from {data['coverage']:.1f}% to 60% for beta"
            )
        elif data["coverage"] < 80 and data["status"] == "beta":
            recommendations.append(
                f"🟡 Increase {provider} coverage from {data['coverage']:.1f}% to 80% for stable"
            )

    # Ready for promotion
    for provider, data in report["providers"].items():
        if (
            data["ready_for_promotion"]["ready"]
            and data["ready_for_promotion"]["next_level"] != data["status"]
        ):
            recommendations.append(
                f"✅ {provider} ready for promotion to {data['ready_for_promotion']['next_level']}"
            )

    # Blockers
    for provider, data in report["providers"].items():
        if data["blockers"]:
            recommendations.append(f"📋 Resolve {len(data['blockers'])} blocker(s) for {provider}")

    return recommendations


def generate_next_steps(report: Dict) -> List[str]:
    """Generate next steps based on build results."""
    steps = []

    if report["test_results"]["failed_tests"]:
        steps.append("1. Review test-failures.txt and fix failing tests")
        steps.append("2. Re-run build to verify fixes")
    else:
        steps.append("1. All tests passing ✅")

    # Provider-specific steps
    for provider, data in report["providers"].items():
        promo = data["ready_for_promotion"]
        if promo["ready"] and promo["next_level"] != data["status"]:
            steps.append(
                f"2. Promote {provider}: Edit features.yaml status: {data['status']} → {promo['next_level']}"
            )

    if report["coverage"]["overall"] < 70:
        steps.append(
            f"3. Increase overall coverage from {report['coverage']['overall']:.1f}% to 70%+"
        )

    return steps


def generate_markdown_report(report: Dict) -> str:
    """Generate human-readable Markdown report."""
    build = report["build"]
    tests = report["test_results"]

    md = f"""# Build Report: {build['tag']}

**Build:** #{build['number']}  
**Profile:** {build['profile']}  
**Branch:** {build['branch']}  
**Commit:** {build['commit'][:8]}  
**Time:** {build['timestamp']}  
**URL:** {build['url']}

---

## 📊 Test Results

- **Total:** {tests['summary'].get('total', 0)} tests
- **Passed:** {tests['summary'].get('passed', 0)} ✅
- **Failed:** {tests['summary'].get('failed', 0)} ❌
- **Duration:** {tests.get('duration', 0):.1f}s

"""

    if tests["failed_tests"]:
        md += "### ❌ Failed Tests\n\n"
        for test in tests["failed_tests"]:
            md += f"- `{test['name']}`\n"
        md += "\n*See test-failures.txt for details*\n\n"

    md += f"""---

## 📈 Coverage

**Overall:** {report['coverage']['overall']:.1f}%

### By Provider

"""

    for provider, cov in report["coverage"]["by_provider"].items():
        status = report["providers"][provider]["status"]
        emoji = "🎯" if status == "stable" else "⚠️" if status == "beta" else "🔧"
        md += f"- {emoji} **{provider}** ({status}): {cov:.1f}%\n"

    if report["coverage"]["uncovered_files"]:
        md += "\n### Files Needing Coverage (<50%)\n\n"
        for file in report["coverage"]["uncovered_files"][:10]:
            md += f"- `{file['file']}`: {file['coverage']:.1f}%\n"

    md += "\n---\n\n## 🎯 Recommendations\n\n"
    for rec in report["recommendations"]:
        md += f"- {rec}\n"

    md += "\n---\n\n## 📋 Next Steps\n\n"
    for step in report["next_steps"]:
        md += f"{step}\n"

    md += "\n---\n\n## 🔧 Provider Status\n\n"
    for provider, data in report["providers"].items():
        promo = data["ready_for_promotion"]
        md += f"### {provider}\n\n"
        md += f"- **Current:** {data['status']}\n"
        md += f"- **Coverage:** {data['coverage']:.1f}%\n"
        md += f"- **Code Size:** {data['code_size']} LOC\n"
        md += f"- **Ready for:** {promo['next_level']}\n"
        md += f"- **Status:** {promo['reason']}\n"
        if data["blockers"]:
            md += f"- **Blockers:** {len(data['blockers'])}\n"
            for blocker in data["blockers"]:
                md += f"  - {blocker}\n"
        md += "\n"

    return md


if __name__ == "__main__":
    import sys

    # Get paths
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    features_path = project_root / "fluid_build" / "features.yaml"
    coverage_path = project_root / "coverage.json"
    test_report_path = project_root / "test-report.json"
    output_dir = project_root / "build-reports"

    # Get build info from environment
    build_info = {
        "BUILD_NUMBER": os.getenv("BUILD_NUMBER", "local"),
        "BUILD_TAG": os.getenv("BUILD_TAG", "local-build"),
        "BUILD_PROFILE": os.getenv("BUILD_PROFILE", "alpha"),
        "GIT_BRANCH": os.getenv("GIT_BRANCH", "unknown"),
        "GIT_COMMIT": os.getenv("GIT_COMMIT", "unknown"),
        "BUILD_URL": os.getenv("BUILD_URL", ""),
    }

    print("📝 Generating comprehensive build report...")

    try:
        report = generate_build_report(
            features_path, coverage_path, test_report_path, output_dir, build_info
        )

        print("\n✅ Report generated successfully")
        print("\n📊 Summary:")
        print(
            f"   Tests: {report['test_results']['summary'].get('passed', 0)}/{report['test_results']['summary'].get('total', 0)} passed"
        )
        print(f"   Coverage: {report['coverage']['overall']:.1f}%")
        print(f"   Recommendations: {len(report['recommendations'])}")

        # Note failures but don't exit - let quality gates handle it
        if report["test_results"]["failed_tests"]:
            print(
                f"\n⚠️  Build has {len(report['test_results']['failed_tests'])} failing tests (see test-failures.txt)"
            )

    except Exception as e:
        print(f"❌ Error generating report: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
