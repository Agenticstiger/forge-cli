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
Provider Maturity Assessment Script

Automatically assesses FLUID Build providers against objective release criteria.
Used in CI/CD pipeline to validate provider quality before release.

Usage:
    python3 scripts/assess_provider.py --provider gcp --level stable
    python3 scripts/assess_provider.py --provider all --level beta
    python3 scripts/assess_provider.py --summary
"""

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Run: pip install pyyaml")
    sys.exit(1)


# Color codes for terminal output
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def colored(text: str, color: str) -> str:
    """Add color to text if terminal supports it."""
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.END}"
    return text


# Maturity level requirements
REQUIREMENTS = {
    "alpha": {
        "architecture": {
            "base_provider": True,
            "plan_method": True,
            "apply_method": True,
            "five_phase": False,
            "auth_report": False,
            "render_method": False,
        },
        "code_size": 500,
        "resource_types": 1,
        "test_coverage": 30,
        "integration_tests": 0,
        "examples": 0,
        "max_blockers": 999,  # Many allowed
    },
    "beta": {
        "architecture": {
            "base_provider": True,
            "plan_method": True,
            "apply_method": True,
            "five_phase": True,
            "auth_report": False,
            "render_method": False,
        },
        "code_size": 2000,
        "resource_types": 3,
        "test_coverage": 70,
        "integration_tests": 3,
        "examples": 3,
        "max_blockers": 5,
    },
    "stable": {
        "architecture": {
            "base_provider": True,
            "plan_method": True,
            "apply_method": True,
            "five_phase": True,
            "auth_report": True,
            "render_method": True,
        },
        "code_size": 3000,
        "resource_types": 5,
        "test_coverage": 90,
        "integration_tests": 10,
        "examples": 10,
        "max_blockers": 0,
    },
}


@dataclass
class AssessmentResult:
    """Result of provider assessment."""

    provider: str
    target_level: str
    current_level: str
    passed: bool
    score: float
    checks: Dict[str, Dict] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)


class ProviderAssessor:
    """Assesses provider maturity against release criteria."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.providers_dir = project_root / "fluid_build" / "providers"
        self.tests_dir = project_root / "tests"
        self.features_yaml = project_root / "fluid_build" / "features.yaml"

        # Load features.yaml
        if self.features_yaml.exists():
            with open(self.features_yaml) as f:
                self.features = yaml.safe_load(f)
        else:
            self.features = {}

    def assess_provider(
        self, provider: str, target_level: str, quiet: bool = False
    ) -> AssessmentResult:
        """Run full assessment of a provider."""
        if not quiet:
            print(f"\n{colored('='*70, Colors.BLUE)}")
            print(f"{colored(f'Assessing Provider: {provider}', Colors.BOLD)}")
            print(f"{colored(f'Target Level: {target_level}', Colors.BOLD)}")
            print(f"{colored('='*70, Colors.BLUE)}\n")

        provider_dir = self.providers_dir / provider
        if not provider_dir.exists():
            if not quiet:
                print(
                    f"{colored('ERROR:', Colors.RED)} Provider directory not found: {provider_dir}"
                )
            return AssessmentResult(
                provider=provider,
                target_level=target_level,
                current_level="unknown",
                passed=False,
                score=0.0,
                recommendations=[f"Provider directory not found: {provider_dir}"],
            )

        requirements = REQUIREMENTS[target_level]
        result = AssessmentResult(
            provider=provider,
            target_level=target_level,
            current_level=self.features.get("providers", {})
            .get(provider, {})
            .get("status", "unknown"),
            passed=False,
            score=0.0,
        )

        total_checks = 0
        passed_checks = 0

        # Architecture checks
        if not quiet:
            print(f"{colored('Architecture Requirements:', Colors.BOLD)}")
        arch_result = self._check_architecture(provider_dir, requirements["architecture"])
        result.checks["architecture"] = arch_result
        for check, status in arch_result.items():
            total_checks += 1
            if status["passed"]:
                passed_checks += 1
                if not quiet:
                    print(f"  {colored('✅', Colors.GREEN)} {check}: {status['message']}")
            else:
                if not quiet:
                    print(f"  {colored('❌', Colors.RED)} {check}: {status['message']}")
                result.recommendations.append(f"Fix architecture: {status['message']}")

        # Code size check
        if not quiet:
            print(f"\n{colored('Code Quality:', Colors.BOLD)}")
        code_size = self._check_code_size(provider_dir)
        total_checks += 1
        if code_size >= requirements["code_size"]:
            passed_checks += 1
            if not quiet:
                print(
                    f"  {colored('✅', Colors.GREEN)} Code size: {code_size} lines (min: {requirements['code_size']})"
                )
        else:
            if not quiet:
                print(
                    f"  {colored('❌', Colors.RED)} Code size: {code_size} lines (min: {requirements['code_size']})"
                )
            result.recommendations.append(
                f"Add {requirements['code_size'] - code_size} more lines of code"
            )
        result.checks["code_size"] = {
            "actual": code_size,
            "required": requirements["code_size"],
            "passed": code_size >= requirements["code_size"],
        }

        # Resource types check
        resource_types = self._check_resource_types(provider_dir)
        total_checks += 1
        if resource_types >= requirements["resource_types"]:
            passed_checks += 1
            if not quiet:
                print(
                    f"  {colored('✅', Colors.GREEN)} Resource types: {resource_types} (min: {requirements['resource_types']})"
                )
        else:
            if not quiet:
                print(
                    f"  {colored('❌', Colors.RED)} Resource types: {resource_types} (min: {requirements['resource_types']})"
                )
            result.recommendations.append(
                f"Implement {requirements['resource_types'] - resource_types} more resource types"
            )
        result.checks["resource_types"] = {
            "actual": resource_types,
            "required": requirements["resource_types"],
            "passed": resource_types >= requirements["resource_types"],
        }

        # Test coverage check
        if not quiet:
            print(f"\n{colored('Testing:', Colors.BOLD)}")
        coverage = self._check_test_coverage(provider)
        total_checks += 1
        if coverage is not None:
            if coverage >= requirements["test_coverage"]:
                passed_checks += 1
                if not quiet:
                    print(
                        f"  {colored('✅', Colors.GREEN)} Test coverage: {coverage}% (min: {requirements['test_coverage']}%)"
                    )
            else:
                if not quiet:
                    print(
                        f"  {colored('❌', Colors.RED)} Test coverage: {coverage}% (min: {requirements['test_coverage']}%)"
                    )
                result.recommendations.append(
                    f"Increase test coverage by {requirements['test_coverage'] - coverage}%"
                )
            result.checks["test_coverage"] = {
                "actual": coverage,
                "required": requirements["test_coverage"],
                "passed": coverage >= requirements["test_coverage"],
            }
        else:
            if not quiet:
                print(
                    f"  {colored('⚠️', Colors.YELLOW)} Test coverage: Unable to measure (pytest-cov not available)"
                )
            result.checks["test_coverage"] = {
                "actual": 0,
                "required": requirements["test_coverage"],
                "passed": False,
            }
            result.recommendations.append("Install pytest-cov to measure test coverage")

        # Integration tests check
        integration_tests = self._check_integration_tests(provider)
        total_checks += 1
        if integration_tests >= requirements["integration_tests"]:
            passed_checks += 1
            if not quiet:
                print(
                    f"  {colored('✅', Colors.GREEN)} Integration tests: {integration_tests} (min: {requirements['integration_tests']})"
                )
        else:
            if not quiet:
                print(
                    f"  {colored('❌', Colors.RED)} Integration tests: {integration_tests} (min: {requirements['integration_tests']})"
                )
            result.recommendations.append(
                f"Add {requirements['integration_tests'] - integration_tests} more integration tests"
            )
        result.checks["integration_tests"] = {
            "actual": integration_tests,
            "required": requirements["integration_tests"],
            "passed": integration_tests >= requirements["integration_tests"],
        }

        # Documentation check
        docs_result = self._check_documentation(provider)
        total_checks += 1
        if docs_result.get("passed", False):
            passed_checks += 1
            if not quiet:
                print(f"  {colored('✅', Colors.GREEN)} Documentation: {docs_result['message']}")
        else:
            if not quiet:
                print(f"  {colored('❌', Colors.RED)} Documentation: {docs_result['message']}")
            result.recommendations.append(docs_result["message"])

        # Blockers check
        blockers = self._check_blockers(provider)
        total_checks += 1
        if blockers <= requirements["max_blockers"]:
            passed_checks += 1
            if not quiet:
                print(
                    f"  {colored('✅', Colors.GREEN)} Blockers: {blockers} (max: {requirements['max_blockers']})"
                )
        else:
            if not quiet:
                print(
                    f"  {colored('❌', Colors.RED)} Blockers: {blockers} (max: {requirements['max_blockers']})"
                )
            result.recommendations.append(
                f"Resolve {blockers - requirements['max_blockers']} blockers"
            )
        result.checks["blockers"] = {
            "actual": blockers,
            "required": requirements["max_blockers"],
            "passed": blockers <= requirements["max_blockers"],
        }

        # Calculate final score
        result.score = (passed_checks / total_checks * 100) if total_checks > 0 else 0
        result.passed = result.score >= 100

        # Print summary
        if not quiet:
            print(f"\n{colored('='*70, Colors.BLUE)}")
            print(f"{colored('Assessment Summary:', Colors.BOLD)}")
            print(f"  Checks Passed: {passed_checks}/{total_checks}")
            print(f"  Score: {result.score:.1f}%")

            if result.passed:
                print(f"\n  {colored('✅ READY', Colors.GREEN)} for {target_level} release")
            else:
                print(f"\n  {colored('❌ NOT READY', Colors.RED)} for {target_level} release")
                if result.recommendations:
                    print(f"\n{colored('Recommendations:', Colors.BOLD)}")
                    for i, rec in enumerate(result.recommendations, 1):
                        print(f"  {i}. {rec}")

            print(f"{colored('='*70, Colors.BLUE)}\n")

        return result

    def _check_architecture(
        self, provider_dir: Path, requirements: Dict[str, bool]
    ) -> Dict[str, Dict]:
        """Check architectural requirements."""
        results = {}

        # Find provider.py file
        provider_file = provider_dir / "provider.py"
        if not provider_file.exists():
            # Try alternative names
            for alt in ["__init__.py", f"{provider_dir.name}.py"]:
                if (provider_dir / alt).exists():
                    provider_file = provider_dir / alt
                    break

        if not provider_file.exists():
            return {"file_exists": {"passed": False, "message": "Provider file not found"}}

        # Read and parse provider file
        try:
            with open(provider_file) as f:
                tree = ast.parse(f.read())
        except Exception as e:
            return {"parse_error": {"passed": False, "message": f"Failed to parse: {e}"}}

        # Find provider class
        provider_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # Check if inherits from BaseProvider
                for base in node.bases:
                    if isinstance(base, ast.Name) and base.id == "BaseProvider":
                        provider_class = node
                        break

        # Check BaseProvider inheritance
        if requirements.get("base_provider"):
            results["base_provider"] = {
                "passed": provider_class is not None,
                "message": (
                    "Inherits from BaseProvider"
                    if provider_class
                    else "Does not inherit from BaseProvider"
                ),
            }

        if provider_class:
            methods = {
                node.name for node in provider_class.body if isinstance(node, ast.FunctionDef)
            }

            # Check for plan() method
            if requirements.get("plan_method"):
                results["plan_method"] = {
                    "passed": "plan" in methods,
                    "message": (
                        "Has plan() method" if "plan" in methods else "Missing plan() method"
                    ),
                }

            # Check for apply() method
            if requirements.get("apply_method"):
                results["apply_method"] = {
                    "passed": "apply" in methods,
                    "message": (
                        "Has apply() method" if "apply" in methods else "Missing apply() method"
                    ),
                }

            # Check for render() method
            if requirements.get("render_method"):
                results["render_method"] = {
                    "passed": "render" in methods,
                    "message": (
                        "Has render() method" if "render" in methods else "Missing render() method"
                    ),
                }

        # Check for 5-phase planning
        if requirements.get("five_phase"):
            plan_dir = provider_dir / "plan"
            planner_file = plan_dir / "planner.py" if plan_dir.exists() else None
            has_five_phase = False

            if planner_file and planner_file.exists():
                try:
                    with open(planner_file) as f:
                        content = f.read()
                        # Look for phase references
                        phases = ["infrastructure", "iam", "build", "expose", "schedule"]
                        has_five_phase = all(phase in content.lower() for phase in phases)
                except Exception:
                    pass

            results["five_phase"] = {
                "passed": has_five_phase,
                "message": (
                    "5-phase planning implemented" if has_five_phase else "Missing 5-phase planning"
                ),
            }

        # Check for auth_report
        if requirements.get("auth_report"):
            util_dir = provider_dir / "util"
            auth_file = util_dir / "auth.py" if util_dir.exists() else None
            has_auth_report = False

            if auth_file and auth_file.exists():
                try:
                    with open(auth_file) as f:
                        content = f.read()
                        has_auth_report = "auth_report" in content or "get_auth_report" in content
                except Exception:
                    pass

            results["auth_report"] = {
                "passed": has_auth_report,
                "message": (
                    "Has auth_report() diagnostics"
                    if has_auth_report
                    else "Missing auth_report() diagnostics"
                ),
            }

        return results

    def _check_code_size(self, provider_dir: Path) -> int:
        """Count lines of Python code in provider directory."""
        total_lines = 0
        for py_file in provider_dir.rglob("*.py"):
            try:
                with open(py_file) as f:
                    total_lines += len(f.readlines())
            except Exception:
                pass
        return total_lines

    def _check_resource_types(self, provider_dir: Path) -> int:
        """Count implemented resource types."""
        actions_dir = provider_dir / "actions"
        if not actions_dir.exists():
            return 0

        # Count Python files in actions directory (excluding __init__.py)
        resource_files = [
            f
            for f in actions_dir.glob("*.py")
            if f.name != "__init__.py" and not f.name.startswith("_")
        ]
        return len(resource_files)

    def _check_test_coverage(self, provider: str) -> Optional[float]:
        """Run pytest to get test coverage."""
        try:
            # Run pytest with coverage
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--cov=fluid_build.providers." + provider,
                    "--cov-report=term-missing",
                    "--quiet",
                    "--no-header",
                    "tests/",
                ],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Parse coverage from output
            for line in result.stdout.split("\n"):
                if "TOTAL" in line:
                    parts = line.split()
                    for part in parts:
                        if "%" in part:
                            return float(part.rstrip("%"))

            return None

        except Exception:
            return None

    def _check_integration_tests(self, provider: str) -> int:
        """Count integration tests for provider."""
        # Look for test files matching provider name
        test_files = list(self.tests_dir.glob(f"*{provider}*.py"))
        test_files.extend(list(self.tests_dir.glob(f"**/*{provider}*.py")))

        # Count test functions
        test_count = 0
        for test_file in set(test_files):
            try:
                with open(test_file) as f:
                    tree = ast.parse(f.read())
                    for node in ast.walk(tree):
                        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                            test_count += 1
            except Exception:
                pass

        return test_count

    def _check_documentation(self, provider_dir: Path, min_examples: int) -> Dict:
        """Check documentation completeness."""
        has_readme = (provider_dir / "README.md").exists()

        # Count example files
        examples_dir = provider_dir / "examples"
        example_count = 0
        if examples_dir.exists():
            example_count = len(list(examples_dir.glob("*.yaml"))) + len(
                list(examples_dir.glob("*.yml"))
            )

        # Also check docs/examples
        docs_examples = provider_dir / "docs" / "examples"
        if docs_examples.exists():
            example_count += len(list(docs_examples.glob("*.yaml"))) + len(
                list(docs_examples.glob("*.yml"))
            )

        passed = has_readme and example_count >= min_examples

        if not has_readme:
            message = "Missing README.md"
        elif example_count < min_examples:
            message = f"Only {example_count} examples (min: {min_examples})"
        else:
            message = f"README + {example_count} examples"

        return {"passed": passed, "message": message, "examples": example_count}

    def _check_blockers(self, provider: str) -> int:
        """Count known blockers from features.yaml."""
        provider_info = self.features.get("providers", {}).get(provider, {})
        blockers = provider_info.get("blockers", [])
        return len(blockers) if isinstance(blockers, list) else 0

    def print_summary(self):
        """Print summary of all providers."""
        print(f"\n{colored('='*70, Colors.BLUE)}")
        print(f"{colored('Provider Maturity Summary', Colors.BOLD)}")
        print(f"{colored('='*70, Colors.BLUE)}\n")

        providers_info = self.features.get("providers", {})

        if not providers_info:
            print("No providers found in features.yaml")
            return

        # Header
        print(f"{'Provider':<15} {'Status':<10} {'Code':<10} {'Tests':<10} {'Docs':<10}")
        print(f"{'-'*15} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

        for provider, info in providers_info.items():
            provider_dir = self.providers_dir / provider
            if not provider_dir.exists():
                continue

            status = info.get("status", "unknown")
            code_size = self._check_code_size(provider_dir)
            test_cov = info.get("test_coverage", 0)
            docs = info.get("docs_complete", False)

            status_colored = {
                "stable": colored("stable", Colors.GREEN),
                "beta": colored("beta", Colors.YELLOW),
                "alpha": colored("alpha", Colors.BLUE),
            }.get(status, status)

            docs_symbol = "✅" if docs else "❌"

            print(
                f"{provider:<15} {status_colored:<20} {code_size:<10} {test_cov}%{' ':<6} {docs_symbol:<10}"
            )

        print(f"\n{colored('='*70, Colors.BLUE)}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Assess FLUID Build provider maturity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Assess single provider
  python3 scripts/assess_provider.py --provider gcp --level stable
  
  # Assess all providers at beta level
  python3 scripts/assess_provider.py --provider all --level beta
  
  # Print summary of all providers
  python3 scripts/assess_provider.py --summary
  
  # Output as JSON
  python3 scripts/assess_provider.py --provider snowflake --level beta --json
        """,
    )

    parser.add_argument(
        "--provider", type=str, help='Provider to assess (or "all" for all providers)'
    )
    parser.add_argument(
        "--level",
        type=str,
        choices=["alpha", "beta", "stable"],
        default="alpha",
        help="Target maturity level (default: alpha)",
    )
    parser.add_argument("--summary", action="store_true", help="Print summary of all providers")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument(
        "--strict", action="store_true", help="Exit with error code if assessment fails"
    )

    args = parser.parse_args()

    # Find project root
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    assessor = ProviderAssessor(project_root)

    # Summary mode
    if args.summary:
        assessor.print_summary()
        return 0

    # Require provider for assessment
    if not args.provider:
        parser.print_help()
        return 1

    results = []
    if args.provider == "all":
        providers_info = assessor.features.get("providers", {})
        for provider in providers_info.keys():
            result = assessor.assess_provider(provider, args.level, quiet=args.json)
            results.append(result)
    else:
        result = assessor.assess_provider(args.provider, args.level, quiet=args.json)
        results.append(result)

    # Output results
    if args.json:
        json_results = [
            {
                "provider": r.provider,
                "target_level": r.target_level,
                "current_level": r.current_level,
                "passed": r.passed,
                "score": r.score,
                "checks": r.checks,
                "recommendations": r.recommendations,
            }
            for r in results
        ]
        print(json.dumps(json_results, indent=2))

    # Exit with error if strict mode and any failed
    if args.strict:
        failed = [r for r in results if not r.passed]
        if failed:
            print(
                f"\n{colored('FAILED:', Colors.RED)} {len(failed)} provider(s) did not meet {args.level} criteria"
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
