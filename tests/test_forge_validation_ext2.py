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

"""Extended tests for forge/core/validation.py: ProjectValidator, ValidationResult."""

import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.forge.core.validation import (
    ProjectValidator,
    ValidationIssue,
    ValidationLevel,
    ValidationResult,
    validate_project,
)

LOG = logging.getLogger("test_forge_validation_ext2")


# ---------------------------------------------------------------------------
# ValidationLevel
# ---------------------------------------------------------------------------


class TestValidationLevel:
    def test_values(self):
        assert ValidationLevel.ERROR.value == "error"
        assert ValidationLevel.WARNING.value == "warning"
        assert ValidationLevel.INFO.value == "info"


# ---------------------------------------------------------------------------
# ValidationIssue
# ---------------------------------------------------------------------------


class TestValidationIssue:
    def test_basic(self):
        issue = ValidationIssue(
            level=ValidationLevel.ERROR,
            message="Test error",
        )
        assert issue.level == ValidationLevel.ERROR
        assert issue.message == "Test error"
        assert issue.file_path is None
        assert issue.line_number is None
        assert issue.suggestion is None

    def test_full(self):
        issue = ValidationIssue(
            level=ValidationLevel.WARNING,
            message="Long line",
            file_path="test.py",
            line_number=42,
            suggestion="Break the line",
        )
        assert issue.file_path == "test.py"
        assert issue.line_number == 42
        assert issue.suggestion == "Break the line"


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_success(self):
        result = ValidationResult(success=True, issues=[])
        assert result.success is True
        assert result.errors == []
        assert result.warnings == []
        assert result.info == []

    def test_with_issues(self):
        issues = [
            ValidationIssue(level=ValidationLevel.ERROR, message="e1"),
            ValidationIssue(level=ValidationLevel.WARNING, message="w1"),
            ValidationIssue(level=ValidationLevel.INFO, message="i1"),
            ValidationIssue(level=ValidationLevel.ERROR, message="e2"),
        ]
        result = ValidationResult(success=False, issues=issues)
        assert len(result.errors) == 2
        assert len(result.warnings) == 1
        assert len(result.info) == 1


# ---------------------------------------------------------------------------
# ProjectValidator
# ---------------------------------------------------------------------------


class TestProjectValidator:
    def _make_project(self, tmpdir, contract=None, files=None):
        base = Path(tmpdir)
        if contract is not None:
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)
        if files:
            for name, content in files.items():
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, str):
                    path.write_text(content)
                elif isinstance(content, dict):
                    with open(path, "w") as f:
                        json.dump(content, f)
        return base

    def test_missing_required_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            validator = ProjectValidator(tmpdir)
            result = validator.validate_project()
            assert not result.success
            assert any("contract.fluid.yaml" in e.message for e in result.errors)

    def test_valid_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {
                    "inputs": [{"name": "input1", "type": "table"}],
                    "outputs": [{"name": "output1", "type": "table"}],
                },
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml>=6.0\n",
                    "src/__init__.py": "",
                    "tests/__init__.py": "",
                    "docs/.gitkeep": "",
                    "config/.gitkeep": "",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            # Should have no errors (may have warnings about missing directories)
            assert len(result.errors) == 0

    def test_invalid_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                contract={"id": "test"},
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            # Missing apiVersion, kind, metadata, spec
            contract_errors = [
                e
                for e in result.errors
                if "contract" in e.message.lower() or "field" in e.message.lower()
            ]
            assert len(contract_errors) > 0

    def test_unknown_api_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "99.0",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            api_warnings = [w for w in result.warnings if "version" in w.message.lower()]
            assert len(api_warnings) > 0

    def test_wrong_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "0.5.7",
                "kind": "UnknownKind",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            kind_warnings = [w for w in result.warnings if "kind" in w.message.lower()]
            assert len(kind_warnings) > 0

    def test_missing_metadata_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            name_errors = [e for e in result.errors if "name" in e.message.lower()]
            assert len(name_errors) > 0

    def test_no_inputs_or_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {},
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            io_errors = [
                e for e in result.errors if "inputs" in e.message or "outputs" in e.message
            ]
            assert len(io_errors) > 0

    def test_inputs_not_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": "not_a_list"},
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            list_errors = [e for e in result.errors if "list" in e.message.lower()]
            assert len(list_errors) > 0

    def test_inputs_item_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"description": "missing name and type"}]},
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            missing_errors = [e for e in result.errors if "missing" in e.message.lower()]
            assert len(missing_errors) > 0

    def test_inputs_item_not_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": ["string_item"]},
            }
            base = self._make_project(
                tmpdir,
                contract=contract,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                },
            )
            validator = ProjectValidator(base)
            result = validator.validate_project()
            type_errors = [e for e in result.errors if "object" in e.message.lower()]
            assert len(type_errors) > 0

    def test_python_syntax_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                    "contract.fluid.yaml": "",
                    "src/bad.py": "def foo(\n",
                },
            )
            # Write contract
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            syntax_issues = [i for i in result.issues if "syntax" in i.message.lower()]
            assert len(syntax_issues) > 0

    def test_long_lines_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                    "src/long.py": "x = " + "'" + "a" * 200 + "'\n",
                },
            )
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            long_warnings = [w for w in result.warnings if "long" in w.message.lower()]
            assert len(long_warnings) > 0

    def test_eval_usage_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                    "src/risky.py": "result = eval('1+1')\n",
                },
            )
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            eval_warnings = [w for w in result.warnings if "eval" in w.message.lower()]
            assert len(eval_warnings) > 0

    def test_json_syntax_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\n",
                    "config/bad.json": "{not valid json",
                },
            )
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            json_errors = [e for e in result.errors if "json" in e.message.lower()]
            assert len(json_errors) > 0

    def test_unpinned_dependencies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml\nrequests\n",
                },
            )
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            unpinned = [
                w
                for w in result.warnings
                if "unpinned" in w.message.lower() or "Unpinned" in w.message
            ]
            assert len(unpinned) > 0

    def test_empty_requirements(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "\n",
                },
            )
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            empty_warnings = [
                w for w in result.warnings if "empty" in w.message.lower() or "Empty" in w.message
            ]
            assert len(empty_warnings) > 0

    def test_missing_recommended_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_project(
                tmpdir,
                files={
                    "README.md": "# Test\n",
                    "requirements.txt": "pyyaml>=6.0\n",
                },
            )
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            dir_warnings = [w for w in result.warnings if "directory" in w.message.lower()]
            assert len(dir_warnings) > 0

    def test_too_many_root_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            for i in range(15):
                (base / f"file_{i}.txt").write_text(f"content {i}")
            (base / "README.md").write_text("# Test\n")
            (base / "requirements.txt").write_text("pyyaml\n")

            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)

            validator = ProjectValidator(base)
            result = validator.validate_project()
            root_warnings = [w for w in result.warnings if "root" in w.message.lower()]
            assert len(root_warnings) > 0


# ---------------------------------------------------------------------------
# validate_project function
# ---------------------------------------------------------------------------


class TestValidateProjectFunc:
    def test_validate_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            contract = {
                "apiVersion": "0.5.7",
                "kind": "DataProduct",
                "metadata": {"name": "test"},
                "spec": {"inputs": [{"name": "i", "type": "t"}]},
            }
            with open(base / "contract.fluid.yaml", "w") as f:
                yaml.dump(contract, f)
            (base / "README.md").write_text("# Test\n")
            (base / "requirements.txt").write_text("pyyaml>=6.0\n")

            result = validate_project(str(base))
            assert isinstance(result, ValidationResult)
