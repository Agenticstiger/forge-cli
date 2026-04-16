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

"""Branch-coverage tests for fluid_build.forge.core.validation"""

import pytest

from fluid_build.forge.core.validation import (
    ProjectValidator,
    ValidationIssue,
    ValidationLevel,
    ValidationResult,
)

# ── Enum tests ──────────────────────────────────────────────────────


class TestValidationLevel:
    def test_error(self):
        assert ValidationLevel.ERROR.value == "error"

    def test_warning(self):
        assert ValidationLevel.WARNING.value == "warning"

    def test_info(self):
        assert ValidationLevel.INFO.value == "info"


# ── Dataclass tests ─────────────────────────────────────────────────


class TestValidationIssue:
    def test_create_minimal(self):
        vi = ValidationIssue(level=ValidationLevel.ERROR, message="bad")
        assert vi.level == ValidationLevel.ERROR
        assert vi.message == "bad"
        assert vi.file_path is None
        assert vi.line_number is None
        assert vi.suggestion is None

    def test_create_full(self):
        vi = ValidationIssue(
            level=ValidationLevel.WARNING,
            message="long line",
            file_path="src/main.py",
            line_number=42,
            suggestion="break it up",
        )
        assert vi.file_path == "src/main.py"
        assert vi.line_number == 42
        assert vi.suggestion == "break it up"


class TestValidationResult:
    def test_success_no_issues(self):
        vr = ValidationResult(success=True, issues=[])
        assert vr.errors == []
        assert vr.warnings == []
        assert vr.info == []

    def test_errors_property(self):
        issues = [
            ValidationIssue(level=ValidationLevel.ERROR, message="e1"),
            ValidationIssue(level=ValidationLevel.WARNING, message="w1"),
            ValidationIssue(level=ValidationLevel.ERROR, message="e2"),
        ]
        vr = ValidationResult(success=False, issues=issues)
        assert len(vr.errors) == 2
        assert all(e.level == ValidationLevel.ERROR for e in vr.errors)

    def test_warnings_property(self):
        issues = [
            ValidationIssue(level=ValidationLevel.WARNING, message="w1"),
            ValidationIssue(level=ValidationLevel.INFO, message="i1"),
        ]
        vr = ValidationResult(success=True, issues=issues)
        assert len(vr.warnings) == 1
        assert vr.warnings[0].message == "w1"

    def test_info_property(self):
        issues = [
            ValidationIssue(level=ValidationLevel.INFO, message="i1"),
            ValidationIssue(level=ValidationLevel.INFO, message="i2"),
        ]
        vr = ValidationResult(success=True, issues=issues)
        assert len(vr.info) == 2


# ── ProjectValidator tests ──────────────────────────────────────────


@pytest.fixture
def valid_project(tmp_path):
    """Create a valid project structure for validation."""
    (tmp_path / "contract.fluid.yaml").write_text(
        "apiVersion: '0.5.7'\nkind: DataProduct\nmetadata:\n  name: test\nspec:\n  inputs:\n    - name: src\n      type: table\n  outputs:\n    - name: dst\n      type: table\n"
    )
    (tmp_path / "README.md").write_text("# Test")
    (tmp_path / "requirements.txt").write_text("requests>=2.0")
    for d in ["src", "tests", "docs", "config"]:
        (tmp_path / d).mkdir()
    return tmp_path


@pytest.fixture
def validator(valid_project):
    return ProjectValidator(valid_project)


class TestProjectValidatorInit:
    def test_sets_project_path(self, validator, valid_project):
        assert validator.project_path == valid_project

    def test_issues_empty(self, validator):
        assert validator.issues == []


class TestValidateStructure:
    def test_missing_contract(self, tmp_path):
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("contract.fluid.yaml" in m for m in error_msgs)

    def test_missing_readme(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("x: 1")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("README.md" in m for m in error_msgs)

    def test_missing_requirements(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("x: 1")
        (tmp_path / "README.md").write_text("# hi")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("requirements.txt" in m for m in error_msgs)

    def test_missing_recommended_dirs(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("x: 1")
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        warn_msgs = [w.message for w in result.warnings]
        assert any("src" in m for m in warn_msgs)
        assert any("tests" in m for m in warn_msgs)

    def test_all_required_present(self, validator):
        result = validator.validate_project()
        error_msgs = [e.message for e in result.errors]
        # Should not have missing file errors
        assert not any("Required file missing" in m for m in error_msgs)


class TestCheckAntiPatterns:
    def test_large_file_warning(self, valid_project):
        # Create a file > 1MB
        big_file = valid_project / "src" / "big.bin"
        big_file.write_bytes(b"x" * (1024 * 1024 + 1))
        v = ProjectValidator(valid_project)
        result = v.validate_project()
        warn_msgs = [w.message for w in result.warnings]
        assert any("Large file" in m for m in warn_msgs)

    def test_too_many_root_files(self, valid_project):
        for i in range(15):
            (valid_project / f"file{i}.txt").write_text("x")
        v = ProjectValidator(valid_project)
        result = v.validate_project()
        warn_msgs = [w.message for w in result.warnings]
        assert any("Too many files in root" in m for m in warn_msgs)

    def test_empty_directory(self, valid_project):
        (valid_project / "empty_dir").mkdir()
        v = ProjectValidator(valid_project)
        result = v.validate_project()
        info_msgs = [i.message for i in result.info]
        assert any("Empty directory" in m for m in info_msgs)


class TestValidateContract:
    def test_valid_contract(self, validator):
        result = validator.validate_project()
        # Should not have contract-related errors
        error_msgs = [e.message for e in result.errors]
        assert not any("Contract missing" in m for m in error_msgs)

    def test_missing_required_fields(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("partial: true")
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("apiVersion" in m for m in error_msgs)
        assert any("kind" in m for m in error_msgs)
        assert any("metadata" in m for m in error_msgs)
        assert any("spec" in m for m in error_msgs)

    def test_unknown_api_version(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '99.0'\nkind: DataProduct\nmetadata:\n  name: test\nspec:\n  inputs:\n    - name: x\n      type: t\n"
        )
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        warn_msgs = [w.message for w in result.warnings]
        assert any("Unknown schema version" in m for m in warn_msgs)

    def test_unexpected_kind(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '0.5.7'\nkind: NotDataProduct\nmetadata:\n  name: test\nspec:\n  inputs:\n    - name: x\n      type: t\n"
        )
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        warn_msgs = [w.message for w in result.warnings]
        assert any("Unexpected kind" in m for m in warn_msgs)

    def test_missing_metadata_name(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '0.5.7'\nkind: DataProduct\nmetadata:\n  version: '1.0'\nspec:\n  inputs:\n    - name: x\n      type: t\n"
        )
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("metadata missing name" in m for m in error_msgs)

    def test_bad_yaml(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(": : invalid yaml {{{")
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("Error reading contract" in m or "contract" in m.lower() for m in error_msgs)


class TestValidateContractCompleteness:
    def test_missing_inputs_and_outputs(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '0.5.7'\nkind: DataProduct\nmetadata:\n  name: test\nspec:\n  mode: batch\n"
        )
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("inputs or outputs" in m for m in error_msgs)

    def test_inputs_not_a_list(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '0.5.7'\nkind: DataProduct\nmetadata:\n  name: test\nspec:\n  inputs: not_a_list\n"
        )
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("must be a list" in m for m in error_msgs)

    def test_io_item_not_dict(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '0.5.7'\nkind: DataProduct\nmetadata:\n  name: test\nspec:\n  outputs:\n    - just_a_string\n"
        )
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("must be an object" in m for m in error_msgs)

    def test_io_item_missing_fields(self, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: '0.5.7'\nkind: DataProduct\nmetadata:\n  name: test\nspec:\n  inputs:\n    - description: no name or type\n"
        )
        (tmp_path / "README.md").write_text("# hi")
        (tmp_path / "requirements.txt").write_text("")
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("missing name" in m for m in error_msgs)
        assert any("missing type" in m for m in error_msgs)


class TestValidateCodeQuality:
    def test_python_syntax_error(self, valid_project):
        (valid_project / "src" / "bad.py").write_text("def f(:\n  pass")
        v = ProjectValidator(valid_project)
        result = v.validate_project()
        error_msgs = [e.message for e in result.errors]
        assert any("syntax error" in m.lower() for m in error_msgs)

    def test_python_long_line(self, valid_project):
        (valid_project / "src" / "long.py").write_text("x = " + "a" * 200)
        v = ProjectValidator(valid_project)
        result = v.validate_project()
        warn_msgs = [w.message for w in result.warnings]
        assert any("Line too long" in m for m in warn_msgs)

    def test_python_eval_warning(self, valid_project):
        (valid_project / "src" / "unsafe.py").write_text("result = eval('1+1')\n")
        v = ProjectValidator(valid_project)
        result = v.validate_project()
        warn_msgs = [w.message for w in result.warnings]
        assert any("eval" in m for m in warn_msgs)


class TestValidateProject:
    def test_valid_project_succeeds(self, validator):
        result = validator.validate_project()
        # May have warnings about empty dirs but no errors from required structure
        struct_errors = [e for e in result.errors if "Required file missing" in e.message]
        assert len(struct_errors) == 0

    def test_has_errors_marks_not_success(self, tmp_path):
        # Empty project: missing all required files
        v = ProjectValidator(tmp_path)
        result = v.validate_project()
        assert result.success is False

    def test_issues_cleared_on_revalidate(self, validator):
        validator.validate_project()
        count1 = len(validator.issues)
        validator.validate_project()
        count2 = len(validator.issues)
        # Issues should be same count (reset each time)
        assert count1 == count2
