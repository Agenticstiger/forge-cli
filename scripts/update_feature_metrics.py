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
Update features.yaml with actual test metrics while preserving manual decisions.

Auto-updates (from real test data):
- test_coverage: Actual % from pytest-cov
- tests_passed/failed/total: Real test counts
- last_tested: Timestamp of last test run
- code_size: Actual LOC count

Manual fields (preserved - you control):
- status: alpha/beta/stable (YOU decide when to promote)
- blockers: What's blocking promotion
- note: Human-readable description
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml


def count_lines_of_code(provider_path: Path) -> int:
    """Count non-blank, non-comment lines in a provider directory."""
    total = 0
    for pyfile in provider_path.rglob("*.py"):
        if "__pycache__" in str(pyfile) or "test" in str(pyfile):
            continue
        try:
            with open(pyfile) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        total += 1
        except Exception:
            pass
    return total


def get_provider_coverage(coverage_data: Dict, provider_name: str) -> Dict[str, Any]:
    """Extract coverage metrics for a specific provider from coverage.json.

    NOTE: We measure provider test quality by running provider-specific tests,
    not by checking if CLI tests happen to execute provider code.

    For export-only providers (odps_standard, odcs, datamesh_manager),
    we measure conversion/export logic coverage.
    """
    if not coverage_data or "files" not in coverage_data:
        return {"coverage": 0, "files": 0, "note": "No coverage data available"}

    provider_files = []

    # Match provider files - must match actual directory structure
    # Pattern: /providers/{name}/ should match any path containing this
    provider_pattern = f"/providers/{provider_name}/"

    all_files_count = len(coverage_data.get("files", {}))

    for file_path, file_data in coverage_data.get("files", {}).items():
        # Normalize path separators
        normalized_path = file_path.replace("\\", "/")

        # Skip test files
        if "/test" in normalized_path or "test_" in normalized_path or "/tests/" in normalized_path:
            continue

        # Check if this file belongs to this provider
        if provider_pattern in normalized_path:
            summary = file_data.get("summary", {})
            provider_files.append(
                {
                    "file": normalized_path,
                    "coverage": summary.get("percent_covered", 0),
                    "lines_covered": summary.get("covered_lines", 0),
                    "lines_missing": summary.get("missing_lines", 0),
                }
            )

    if not provider_files:
        # Debug: show what files were checked
        sample_files = list(coverage_data.get("files", {}).keys())[:3]
        return {
            "coverage": 0,
            "files": 0,
            "note": f"No files matched pattern '{provider_pattern}' (checked {all_files_count} files)",
        }

    # Calculate weighted average coverage
    total_lines = sum(f["lines_covered"] + f["lines_missing"] for f in provider_files)
    if total_lines == 0:
        return {"coverage": 0, "files": len(provider_files), "note": "No executable lines"}

    total_covered = sum(f["lines_covered"] for f in provider_files)
    avg_coverage = (total_covered / total_lines) * 100

    return {
        "coverage": round(avg_coverage, 1),
        "files": len(provider_files),
        "total_lines": total_lines,
        "covered_lines": total_covered,
    }


def get_test_results(test_report_path: Path) -> Dict[str, int]:
    """Extract test pass/fail counts from pytest JSON report."""
    if not test_report_path.exists():
        return {"passed": 0, "failed": 0, "total": 0}

    with open(test_report_path) as f:
        report = json.load(f)

    summary = report.get("summary", {})
    return {
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "total": summary.get("total", 0),
    }


def update_features_yaml(
    features_path: Path,
    coverage_path: Path,
    test_report_path: Path,
    providers_base_path: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Update features.yaml with real metrics, preserving manual fields."""

    # Load current features.yaml
    with open(features_path) as f:
        features = yaml.safe_load(f)

    # Load coverage data
    coverage_data = {}
    if coverage_path.exists():
        with open(coverage_path) as f:
            coverage_data = json.load(f)
        print(f"📊 Loaded coverage data from {coverage_path}")
        print(f"   Found {len(coverage_data.get('files', {}))} files with coverage info")
    else:
        print(f"❌ No coverage data found at {coverage_path}")

    # Load test results
    test_results = get_test_results(test_report_path)

    # Get overall coverage
    overall_coverage = coverage_data.get("totals", {}).get("percent_covered", 0)

    timestamp = datetime.utcnow().isoformat() + "Z"

    updates = {
        "overall": {
            "coverage": round(overall_coverage, 1),
            "tests": test_results,
            "timestamp": timestamp,
        },
        "providers": {},
    }

    # Update each provider with real metrics
    for provider_name, provider_data in features["providers"].items():
        # Calculate real metrics
        provider_path = providers_base_path / provider_name

        # Get actual coverage for this provider
        provider_cov = get_provider_coverage(coverage_data, provider_name)

        # Debug output
        if provider_cov.get("note"):
            print(f"  ⚠️  {provider_name}: {provider_cov['note']}")
        elif provider_cov["files"] > 0:
            print(
                f"  ✅ {provider_name}: {provider_cov['coverage']:.1f}% ({provider_cov['files']} files)"
            )
        else:
            print(f"  ⚠️  {provider_name}: No coverage data")

        # Count lines of code
        loc = count_lines_of_code(provider_path) if provider_path.exists() else 0

        # PRESERVE manual fields (you control these)
        # NOTE: test_coverage is ALSO manual now because pytest coverage
        # measures "code executed by all tests" not "provider test quality"
        # When you add 200 CLI tests, provider coverage drops even though
        # provider tests haven't changed. So we keep coverage manual.
        manual_fields = {
            "status": provider_data.get("status", "alpha"),
            "test_coverage": provider_data.get("test_coverage", 0),  # MANUAL - don't auto-update
            "blockers": provider_data.get("blockers", []),
            "note": provider_data.get("note", ""),
            "resource_types": provider_data.get("resource_types", []),
            "architecture": provider_data.get("architecture", {}),
            "docs_complete": provider_data.get("docs_complete", False),
        }

        # UPDATE automatic fields (from real data)
        # We still calculate coverage for INFO but don't store it
        auto_fields = {
            "code_size": loc,
            "last_tested": timestamp,
        }

        # Add test breakdown if available
        if test_results["total"] > 0:
            auto_fields["tests"] = {
                "passed": test_results["passed"],
                "failed": test_results["failed"],
                "total": test_results["total"],
            }

        # Merge: manual fields take precedence
        updated_provider = {**auto_fields, **manual_fields}

        # Update in features dict
        features["providers"][provider_name] = updated_provider

        updates["providers"][provider_name] = {
            "coverage_change": 0,  # Not auto-updating coverage anymore
            "loc_change": loc - provider_data.get("code_size", 0),
            "new_coverage": provider_data.get("test_coverage", 0),  # Keep existing manual value
            "new_loc": loc,
            "measured_coverage": provider_cov["coverage"],  # For info only
        }

    # Update metadata
    features["metadata"]["last_updated"] = timestamp
    features["metadata"]["updated_by"] = "jenkins-ci"

    if not dry_run:
        # Write updated features.yaml
        with open(features_path, "w") as f:
            yaml.dump(features, f, default_flow_style=False, sort_keys=False, indent=2)
        print(f"✅ Updated {features_path}")
    else:
        print(f"🔍 DRY RUN - would update {features_path}")

    return updates


def print_summary(updates: Dict[str, Any]):
    """Print a summary of what changed."""
    print("\n" + "=" * 70)
    print("  FEATURE METRICS UPDATE SUMMARY")
    print("=" * 70)

    overall = updates["overall"]
    print("\n📊 Overall:")
    print(f"  Coverage: {overall['coverage']:.1f}%")
    print(f"  Tests: {overall['tests']['passed']}/{overall['tests']['total']} passed")
    print(f"  Updated: {overall['timestamp']}")

    print("\n🔧 Provider Updates:")
    print("-" * 70)
    print(f"{'Provider':<15} {'Coverage (Manual)':<20} {'LOC':<20} {'Action'}")
    print("-" * 70)

    for provider, changes in updates["providers"].items():
        loc_change = changes["loc_change"]
        measured = changes.get("measured_coverage", 0)

        # Show manual coverage value (what's actually stored)
        cov_str = f"{changes['new_coverage']:.1f}%"
        loc_str = f"{changes['new_loc']} ({loc_change:+d})"

        # Info: also show what pytest measured (for comparison)
        if measured > 0 and measured != changes["new_coverage"]:
            cov_str += f" (pytest: {measured:.1f}%)"

        # Suggest action based on MANUAL metrics in features.yaml
        if changes["new_coverage"] >= 80:
            action = "✅ Ready for stable"
        elif changes["new_coverage"] >= 30:
            action = "⚠️  Ready for beta"
        else:
            action = "🔧 Keep in alpha"

        print(f"{provider:<15} {cov_str:<20} {loc_str:<20} {action}")

    print("-" * 70)
    print("\n💡 Manual Actions Required:")
    print("   Provider test_coverage is now MANUAL (not auto-updated)")
    print("   Reason: pytest coverage measures 'code executed' not 'test quality'")
    print("   Update test_coverage in features.yaml when you add provider tests")
    print("   Example: After adding 10 provider tests → manually set coverage: 85")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    import sys

    # Get paths
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    features_path = project_root / "fluid_build" / "features.yaml"
    coverage_path = project_root / "coverage.json"
    test_report_path = project_root / "test-report.json"
    providers_path = project_root / "fluid_build" / "providers"

    # Check for dry-run flag
    dry_run = "--dry-run" in sys.argv

    if not features_path.exists():
        print(f"❌ features.yaml not found at {features_path}")
        sys.exit(1)

    print("🔄 Updating feature metrics from test results...")

    try:
        updates = update_features_yaml(
            features_path, coverage_path, test_report_path, providers_path, dry_run=dry_run
        )

        print_summary(updates)

        if dry_run:
            print("\n🔍 Dry run complete - no files modified")
        else:
            print("\n✅ features.yaml updated with real metrics")
            print("   Manual 'status' fields preserved - review and update when ready")

    except Exception as e:
        print(f"❌ Error updating metrics: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
