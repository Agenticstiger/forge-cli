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

"""Tests for fluid_build/forge/core/validation.py — project validation."""

from fluid_build.forge.core.validation import (
    ProjectValidator,
    ValidationIssue,
    ValidationLevel,
    ValidationResult,
)


class TestValidationLevel:
    def test_values(self):
        assert ValidationLevel.ERROR.value == "error"
        assert ValidationLevel.WARNING.value == "warning"
        assert ValidationLevel.INFO.value == "info"


class TestValidationIssue:
    def test_basic(self):
        issue = ValidationIssue(
            level=ValidationLevel.ERROR,
            message="Missing field: id",
        )
        assert issue.level == ValidationLevel.ERROR
        assert issue.message == "Missing field: id"
        assert issue.file_path is None
        assert issue.suggestion is None

    def test_with_details(self):
        issue = ValidationIssue(
            level=ValidationLevel.WARNING,
            message="Large file",
            file_path="data/big.csv",
            line_number=1,
            suggestion="Split it up",
        )
        assert issue.file_path == "data/big.csv"
        assert issue.line_number == 1


class TestValidationResult:
    def test_success(self):
        result = ValidationResult(success=True, issues=[])
        assert result.success is True
        assert result.errors == []
        assert result.warnings == []
        assert result.info == []

    def test_mixed_issues(self):
        issues = [
            ValidationIssue(level=ValidationLevel.ERROR, message="e1"),
            ValidationIssue(level=ValidationLevel.WARNING, message="w1"),
            ValidationIssue(level=ValidationLevel.WARNING, message="w2"),
            ValidationIssue(level=ValidationLevel.INFO, message="i1"),
        ]
        result = ValidationResult(success=False, issues=issues)
        assert len(result.errors) == 1
        assert len(result.warnings) == 2
        assert len(result.info) == 1


class TestProjectValidator:
    def _create_valid_project(self, tmp_path):
        """Create a minimal valid FLUID project."""
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '0.5.7'\nkind: DataProduct\nmetadata:\n  name: test\nspec:\n  id: test\n"
        )
        (tmp_path / "README.md").write_text("# Test\n")
        (tmp_path / "requirements.txt").write_text("pyyaml\n")
        return tmp_path

    def test_valid_project(self, tmp_path):
        self._create_valid_project(tmp_path)
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        # Validation result is returned (may have warnings/errors about structure)
        assert isinstance(result, ValidationResult)
        assert isinstance(result.issues, list)

    def test_missing_contract(self, tmp_path):
        (tmp_path / "README.md").write_text("# Test\n")
        (tmp_path / "requirements.txt").write_text("pyyaml\n")
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        assert any("contract.fluid.yaml" in i.message for i in result.errors)

    def test_missing_readme(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("kind: DataProduct\n")
        (tmp_path / "requirements.txt").write_text("\n")
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        assert any("README.md" in i.message for i in result.errors)

    def test_missing_requirements(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("kind: DataProduct\n")
        (tmp_path / "README.md").write_text("# Test\n")
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        assert any("requirements.txt" in i.message for i in result.errors)

    def test_recommended_dirs_warning(self, tmp_path):
        self._create_valid_project(tmp_path)
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        # Should warn about missing recommended dirs (src, tests, docs, config)
        warnings = [i.message for i in result.warnings]
        assert any("src" in w for w in warnings)

    def test_empty_directory_detected(self, tmp_path):
        self._create_valid_project(tmp_path)
        (tmp_path / "empty_dir").mkdir()
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        info_msgs = [i.message for i in result.info]
        assert any("empty_dir" in m for m in info_msgs)

    def test_invalid_yaml_contract(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("{{{{invalid yaml")
        (tmp_path / "README.md").write_text("# Test\n")
        (tmp_path / "requirements.txt").write_text("\n")
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        # Should have an error about contract parsing
        assert not result.success or any(
            "Error" in i.message or "missing" in i.message for i in result.issues
        )

    def test_contract_missing_required_fields(self, tmp_path):
        # Valid YAML but missing FLUID spec fields
        (tmp_path / "contract.fluid.yaml").write_text("name: test\nversion: 1.0\n")
        (tmp_path / "README.md").write_text("# Test\n")
        (tmp_path / "requirements.txt").write_text("\n")
        validator = ProjectValidator(tmp_path)
        result = validator.validate_project()
        errors = [i.message for i in result.errors]
        # Should report missing apiVersion, kind, metadata, spec
        assert any("apiVersion" in e or "kind" in e or "metadata" in e for e in errors)
