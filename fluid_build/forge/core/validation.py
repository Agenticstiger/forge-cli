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
Project validation system for FLUID Forge

This module provides validation for generated projects to ensure they:
1. Follow FLUID specification correctly
2. Have valid file structures
3. Can be built and deployed successfully
4. Meet quality standards
"""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

try:
    import yaml
except ImportError:
    # Fallback YAML implementation using json
    class _YamlFallback:
        def safe_load(self, f):
            # Basic YAML subset support
            content = f.read() if hasattr(f, "read") else f
            if isinstance(content, str):
                # Simple YAML to JSON conversion for basic cases
                lines = content.strip().split("\n")
                result = {}
                for line in lines:
                    if ":" in line and not line.strip().startswith("#"):
                        key, value = line.split(":", 1)
                        key = key.strip()
                        value = value.strip().strip("\"'")
                        result[key] = value
                return result
            return {}

        def dump(self, data, f, **kwargs):
            import json

            json.dump(data, f, indent=2)

    yaml = _YamlFallback()


class ValidationLevel(Enum):
    """Validation severity levels"""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """A validation issue found in a project"""

    level: ValidationLevel
    message: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    suggestion: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of project validation"""

    success: bool
    issues: List[ValidationIssue]

    @property
    def errors(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == ValidationLevel.ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == ValidationLevel.WARNING]

    @property
    def info(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == ValidationLevel.INFO]


class ProjectValidator:
    """Comprehensive project validator"""

    def __init__(self, project_path: Path):
        self.project_path = Path(project_path)
        self.issues: List[ValidationIssue] = []

    def validate_project(self) -> ValidationResult:
        """Run comprehensive validation on the project"""
        self.issues = []

        # Basic structure validation
        self._validate_structure()

        # FLUID contract validation
        self._validate_contract()

        # Code quality validation
        self._validate_code_quality()

        # Dependency validation
        self._validate_dependencies()

        # Documentation validation
        self._validate_documentation()

        # Security validation
        self._validate_security()

        # Determine overall success
        has_errors = any(issue.level == ValidationLevel.ERROR for issue in self.issues)

        return ValidationResult(success=not has_errors, issues=self.issues)

    def _validate_structure(self):
        """Validate project structure follows conventions"""

        # Check required files exist
        required_files = ["contract.fluid.yaml", "README.md", "requirements.txt"]

        for required_file in required_files:
            file_path = self.project_path / required_file
            if not file_path.exists():
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.ERROR,
                        message=f"Required file missing: {required_file}",
                        suggestion=f"Create {required_file} with appropriate content",
                    )
                )

        # Check recommended directories
        recommended_dirs = ["src", "tests", "docs", "config"]

        for recommended_dir in recommended_dirs:
            dir_path = self.project_path / recommended_dir
            if not dir_path.exists():
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.WARNING,
                        message=f"Recommended directory missing: {recommended_dir}",
                        suggestion=f"Consider creating {recommended_dir}/ for better organization",
                    )
                )

        # Check for common anti-patterns
        self._check_anti_patterns()

    def _check_anti_patterns(self):
        """Check for common project anti-patterns"""

        # Check for large files
        for file_path in self.project_path.rglob("*"):
            if file_path.is_file() and file_path.stat().st_size > 1024 * 1024:  # 1MB
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.WARNING,
                        message=f"Large file detected: {file_path.name} ({file_path.stat().st_size / 1024 / 1024:.1f}MB)",
                        file_path=str(file_path.relative_to(self.project_path)),
                        suggestion="Consider splitting large files or using external storage",
                    )
                )

        # Check for too many files in root
        root_files = [f for f in self.project_path.iterdir() if f.is_file()]
        if len(root_files) > 10:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.WARNING,
                    message=f"Too many files in root directory ({len(root_files)})",
                    suggestion="Consider organizing files into subdirectories",
                )
            )

        # Check for empty directories
        for dir_path in self.project_path.rglob("*"):
            if dir_path.is_dir() and not any(dir_path.iterdir()):
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.INFO,
                        message=f"Empty directory: {dir_path.relative_to(self.project_path)}",
                        suggestion="Consider adding a .gitkeep file or removing empty directory",
                    )
                )

    def _validate_contract(self):
        """Validate FLUID contract"""
        contract_path = self.project_path / "contract.fluid.yaml"

        if not contract_path.exists():
            return  # Already reported in structure validation

        try:
            with open(contract_path) as f:
                contract = yaml.safe_load(f)

            # Check FLUID spec compliance
            self._validate_fluid_spec(contract)

            # Check contract completeness
            self._validate_contract_completeness(contract)

        except Exception as e:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.ERROR,
                    message=f"Error reading contract file: {str(e)}",
                    file_path="contract.fluid.yaml",
                    suggestion="Fix YAML syntax errors in contract file",
                )
            )

    def _validate_fluid_spec(self, contract: Dict[str, Any]):
        """Validate contract follows FLUID specification"""

        # Check required top-level fields
        required_fields = ["apiVersion", "kind", "metadata", "spec"]
        for field in required_fields:
            if field not in contract:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.ERROR,
                        message=f"Contract missing required field: {field}",
                        file_path="contract.fluid.yaml",
                        suggestion=f"Add {field} field to contract",
                    )
                )

        # Validate apiVersion / fluidVersion
        declared_version = contract.get("apiVersion") or contract.get("fluidVersion")
        if declared_version:
            try:
                from fluid_build.schema_manager import FluidSchemaManager

                known_versions = set(FluidSchemaManager.BUNDLED_VERSIONS)
            except Exception:  # noqa: BLE001
                # Defensive fallback when the schema manager is unavailable;
                # the canonical 0.7.x line is the supported set.
                known_versions = {"0.7.1", "0.7.2", "0.7.3"}
            if declared_version not in known_versions:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.WARNING,
                        message=f"Unknown schema version: {declared_version}",
                        file_path="contract.fluid.yaml",
                        suggestion=f"Use supported version: {', '.join(sorted(known_versions))}",
                    )
                )

        # Validate kind
        if "kind" in contract:
            kind = contract["kind"]
            if kind != "DataProduct":
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.WARNING,
                        message=f"Unexpected kind: {kind}",
                        file_path="contract.fluid.yaml",
                        suggestion="Use 'DataProduct' as kind for data products",
                    )
                )

        # Validate metadata
        if "metadata" in contract:
            metadata = contract["metadata"]
            if "name" not in metadata:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.ERROR,
                        message="Contract metadata missing name",
                        file_path="contract.fluid.yaml",
                        suggestion="Add name field to metadata",
                    )
                )

    def _validate_contract_completeness(self, contract: Dict[str, Any]):
        """Validate contract is complete and well-formed"""

        spec = contract.get("spec", {})

        # Check for inputs or outputs
        if "inputs" not in spec and "outputs" not in spec:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.ERROR,
                    message="Contract spec must define inputs or outputs",
                    file_path="contract.fluid.yaml",
                    suggestion="Add inputs or outputs section to spec",
                )
            )

        # Validate inputs structure
        if "inputs" in spec:
            self._validate_contract_io(spec["inputs"], "inputs")

        # Validate outputs structure
        if "outputs" in spec:
            self._validate_contract_io(spec["outputs"], "outputs")

    def _validate_contract_io(self, io_spec: List[Dict[str, Any]], io_type: str):
        """Validate input/output specifications"""

        if not isinstance(io_spec, list):
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.ERROR,
                    message=f"Contract {io_type} must be a list",
                    file_path="contract.fluid.yaml",
                    suggestion=f"Make {io_type} a list of objects",
                )
            )
            return

        for i, item in enumerate(io_spec):
            if not isinstance(item, dict):
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.ERROR,
                        message=f"Contract {io_type}[{i}] must be an object",
                        file_path="contract.fluid.yaml",
                    )
                )
                continue

            # Check required fields for inputs/outputs
            required_fields = ["name", "type"]
            for field in required_fields:
                if field not in item:
                    self.issues.append(
                        ValidationIssue(
                            level=ValidationLevel.ERROR,
                            message=f"Contract {io_type}[{i}] missing {field}",
                            file_path="contract.fluid.yaml",
                            suggestion=f"Add {field} field to {io_type} item",
                        )
                    )

    def _validate_code_quality(self):
        """Validate code quality and syntax"""

        # Check Python files
        for py_file in self.project_path.rglob("*.py"):
            self._validate_python_file(py_file)

        # Check YAML files
        for yaml_file in self.project_path.rglob("*.yaml"):
            self._validate_yaml_file(yaml_file)

        # Check JSON files
        for json_file in self.project_path.rglob("*.json"):
            self._validate_json_file(json_file)

    def _validate_python_file(self, file_path: Path):
        """Validate Python file syntax and quality"""

        try:
            with open(file_path) as f:
                content = f.read()

            # Check syntax
            try:
                compile(content, str(file_path), "exec")
            except SyntaxError as e:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.ERROR,
                        message=f"Python syntax error: {str(e)}",
                        file_path=str(file_path.relative_to(self.project_path)),
                        line_number=e.lineno,
                        suggestion="Fix Python syntax errors",
                    )
                )

            # Check for common issues
            lines = content.splitlines()
            for i, line in enumerate(lines, 1):
                # Check for overly long lines
                if len(line) > 120:
                    self.issues.append(
                        ValidationIssue(
                            level=ValidationLevel.WARNING,
                            message=f"Line too long ({len(line)} chars)",
                            file_path=str(file_path.relative_to(self.project_path)),
                            line_number=i,
                            suggestion="Break long lines for better readability",
                        )
                    )

                # Check for potential security issues
                if "eval(" in line or "exec(" in line:
                    self.issues.append(
                        ValidationIssue(
                            level=ValidationLevel.WARNING,
                            message="Potential security risk: eval/exec usage",
                            file_path=str(file_path.relative_to(self.project_path)),
                            line_number=i,
                            suggestion="Avoid eval/exec for security reasons",
                        )
                    )

        except Exception as e:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.WARNING,
                    message=f"Could not validate Python file: {str(e)}",
                    file_path=str(file_path.relative_to(self.project_path)),
                )
            )

    def _validate_yaml_file(self, file_path: Path):
        """Validate YAML file syntax"""

        try:
            with open(file_path) as f:
                yaml.safe_load(f)
        except Exception as e:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.ERROR,
                    message=f"YAML syntax error: {str(e)}",
                    file_path=str(file_path.relative_to(self.project_path)),
                    suggestion="Fix YAML syntax errors",
                )
            )

    def _validate_json_file(self, file_path: Path):
        """Validate JSON file syntax"""

        try:
            with open(file_path) as f:
                json.load(f)
        except Exception as e:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.ERROR,
                    message=f"JSON syntax error: {str(e)}",
                    file_path=str(file_path.relative_to(self.project_path)),
                    suggestion="Fix JSON syntax errors",
                )
            )

    def _validate_dependencies(self):
        """Validate project dependencies"""

        requirements_path = self.project_path / "requirements.txt"
        if not requirements_path.exists():
            return

        try:
            with open(requirements_path) as f:
                requirements = f.read().strip().splitlines()

            # Check for empty requirements
            if not requirements:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.WARNING,
                        message="Empty requirements.txt file",
                        file_path="requirements.txt",
                        suggestion="Add required dependencies or remove empty file",
                    )
                )

            # Check for version pinning
            unpinned = []
            for req in requirements:
                req = req.strip()
                if req and not req.startswith("#"):
                    if not any(op in req for op in ["==", ">=", "<=", ">", "<", "~="]):
                        unpinned.append(req)

            if unpinned:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.WARNING,
                        message=f"Unpinned dependencies: {', '.join(unpinned)}",
                        file_path="requirements.txt",
                        suggestion="Pin dependency versions for reproducible builds",
                    )
                )

        except Exception as e:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.WARNING,
                    message=f"Could not validate requirements.txt: {str(e)}",
                    file_path="requirements.txt",
                )
            )

    def _validate_documentation(self):
        """Validate project documentation"""

        readme_path = self.project_path / "README.md"
        if not readme_path.exists():
            return

        try:
            with open(readme_path) as f:
                content = f.read()

            # Check README is not empty
            if len(content.strip()) < 100:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.WARNING,
                        message="README.md is very short",
                        file_path="README.md",
                        suggestion="Add more detailed project description",
                    )
                )

            # Check for basic sections
            expected_sections = ["installation", "usage", "example"]
            missing_sections = []

            for section in expected_sections:
                if section.lower() not in content.lower():
                    missing_sections.append(section)

            if missing_sections:
                self.issues.append(
                    ValidationIssue(
                        level=ValidationLevel.INFO,
                        message=f"README missing recommended sections: {', '.join(missing_sections)}",
                        file_path="README.md",
                        suggestion="Consider adding installation, usage, and example sections",
                    )
                )

        except Exception as e:
            self.issues.append(
                ValidationIssue(
                    level=ValidationLevel.WARNING,
                    message=f"Could not validate README.md: {str(e)}",
                    file_path="README.md",
                )
            )

    def _validate_security(self):
        """Validate project security"""

        # Check for common security issues
        for file_path in self.project_path.rglob("*"):
            if file_path.is_file():
                try:
                    with open(file_path, encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    # Check for hardcoded secrets
                    secret_patterns = ["password", "secret", "key", "token", "api_key"]

                    lines = content.splitlines()
                    for i, line in enumerate(lines, 1):
                        line_lower = line.lower()
                        for pattern in secret_patterns:
                            if (
                                pattern in line_lower
                                and "=" in line
                                and not line.strip().startswith("#")
                                and "example" not in line_lower
                                and "placeholder" not in line_lower
                            ):

                                self.issues.append(
                                    ValidationIssue(
                                        level=ValidationLevel.WARNING,
                                        message=f"Potential hardcoded secret: {pattern}",
                                        file_path=str(file_path.relative_to(self.project_path)),
                                        line_number=i,
                                        suggestion="Use environment variables for secrets",
                                    )
                                )

                except Exception:
                    # Skip binary files or unreadable files
                    continue


def validate_project(project_path: str) -> ValidationResult:
    """Convenience function to validate a project"""
    validator = ProjectValidator(Path(project_path))
    return validator.validate_project()


def print_validation_report(result: ValidationResult, project_path: str):
    """Print a formatted validation report"""

    cprint(f"\n{'='*60}")
    cprint(f"VALIDATION REPORT: {project_path}")
    cprint(f"{'='*60}")

    if result.success:
        success("VALIDATION PASSED")
    else:
        console_error("VALIDATION FAILED")

    cprint("\nSummary:")
    cprint(f"  Errors:   {len(result.errors)}")
    cprint(f"  Warnings: {len(result.warnings)}")
    cprint(f"  Info:     {len(result.info)}")

    # Print errors
    if result.errors:
        cprint(f"\n🔴 ERRORS ({len(result.errors)}):")
        for issue in result.errors:
            location = (
                f" ({issue.file_path}"
                + (f":{issue.line_number}" if issue.line_number else "")
                + ")"
                if issue.file_path
                else ""
            )
            cprint(f"  • {issue.message}{location}")
            if issue.suggestion:
                cprint(f"    💡 {issue.suggestion}")

    # Print warnings
    if result.warnings:
        cprint(f"\n🟡 WARNINGS ({len(result.warnings)}):")
        for issue in result.warnings:
            location = (
                f" ({issue.file_path}"
                + (f":{issue.line_number}" if issue.line_number else "")
                + ")"
                if issue.file_path
                else ""
            )
            cprint(f"  • {issue.message}{location}")
            if issue.suggestion:
                cprint(f"    💡 {issue.suggestion}")

    # Print info
    if result.info:
        cprint(f"\n🔵 INFO ({len(result.info)}):")
        for issue in result.info:
            location = (
                f" ({issue.file_path}"
                + (f":{issue.line_number}" if issue.line_number else "")
                + ")"
                if issue.file_path
                else ""
            )
            cprint(f"  • {issue.message}{location}")
            if issue.suggestion:
                cprint(f"    💡 {issue.suggestion}")

    cprint()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        project_path = sys.argv[1]
        result = validate_project(project_path)
        print_validation_report(result, project_path)
    else:
        cprint("Usage: python validation.py <project_path>")
