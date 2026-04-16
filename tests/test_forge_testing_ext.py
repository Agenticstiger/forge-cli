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

"""Extended tests for forge/core/testing.py: TestResult, TemplateTestSuite, ForgeTestRunner."""

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.forge.core.testing import (
    ForgeTestRunner,
    TemplateTestSuite,
    TestResult,
)

LOG = logging.getLogger("test_forge_testing_ext")


# ---------------------------------------------------------------------------
# TestResult
# ---------------------------------------------------------------------------


class TestTestResult:
    def test_success(self):
        result = TestResult(
            success=True,
            errors=[],
            warnings=[],
            generated_files=["file1.py"],
            execution_time=0.5,
        )
        assert result.success is True
        assert len(result.errors) == 0

    def test_failure(self):
        result = TestResult(
            success=False,
            errors=["Error 1", "Error 2"],
            warnings=["Warning 1"],
            generated_files=[],
            execution_time=1.0,
        )
        assert result.success is False
        assert len(result.errors) == 2
        assert len(result.warnings) == 1

    def test_execution_time(self):
        result = TestResult(
            success=True,
            errors=[],
            warnings=[],
            generated_files=[],
            execution_time=2.5,
        )
        assert result.execution_time == 2.5


# ---------------------------------------------------------------------------
# TemplateTestSuite
# ---------------------------------------------------------------------------


class TestTemplateTestSuite:
    @patch("fluid_build.forge.core.testing.get_template_registry")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    def test_template_not_found(self, _mock_prov_reg, mock_tmpl_reg):
        mock_tmpl_reg.return_value = MagicMock()
        mock_tmpl_reg.return_value.get.return_value = None

        suite = TemplateTestSuite("nonexistent")
        result = suite.run_full_test({})

        assert result.success is False
        assert any("not found" in e for e in result.errors)

    @patch("fluid_build.forge.core.testing.get_template_registry")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    def test_template_metadata_error(self, _mock_prov_reg, mock_tmpl_reg):
        mock_template_cls = MagicMock()
        mock_template = MagicMock()
        mock_template.get_metadata.side_effect = Exception("meta error")
        mock_template_cls.return_value = mock_template
        mock_tmpl_reg.return_value.get.return_value = mock_template_cls

        suite = TemplateTestSuite("test_template")
        result = suite.run_full_test({})

        assert any("meta" in str(e).lower() or "error" in str(e).lower() for e in result.errors)

    @patch("fluid_build.forge.core.testing.get_template_registry")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    def test_unexpected_exception(self, _mock_prov_reg, mock_tmpl_reg):
        mock_tmpl_reg.return_value.get.side_effect = Exception("unexpected")

        suite = TemplateTestSuite("test")
        result = suite.run_full_test({})

        assert result.success is False
        assert any("Unexpected" in e for e in result.errors)

    def test_create_project_structure_files(self):
        suite = TemplateTestSuite.__new__(TemplateTestSuite)
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            structure = {
                "file1.py": "print('hello')",
                "subdir": {"file2.py": "print('world')"},
                "empty_dir": [],
            }
            created = suite._create_project_structure(base, structure)
            assert any("file1.py" in f for f in created)
            assert any("file2.py" in f for f in created)
            assert (base / "empty_dir").is_dir()

    def test_test_file_validation_missing_files(self):
        suite = TemplateTestSuite.__new__(TemplateTestSuite)
        with tempfile.TemporaryDirectory() as tmpdir:
            errors = suite._test_file_validation(Path(tmpdir))
            assert any("contract.fluid.yaml" in e for e in errors)

    def test_test_file_validation_with_files(self):
        suite = TemplateTestSuite.__new__(TemplateTestSuite)
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "contract.fluid.yaml").write_text("apiVersion: '0.5.7'\nkind: DataProduct\n")
            (base / "README.md").write_text("# Test\n")
            (base / "requirements.txt").write_text("pyyaml\n")
            errors = suite._test_file_validation(base)
            # No missing file errors for these three
            missing_errors = [e for e in errors if "Required file missing" in e]
            assert len(missing_errors) == 0

    def test_test_contract_validation_missing(self):
        suite = TemplateTestSuite.__new__(TemplateTestSuite)
        with tempfile.TemporaryDirectory() as tmpdir:
            errors = suite._test_contract_validation(Path(tmpdir))
            assert any("not found" in e for e in errors)

    def test_test_contract_validation_invalid(self):
        suite = TemplateTestSuite.__new__(TemplateTestSuite)
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "contract.fluid.yaml").write_text("not: valid\ncontract: true\n")
            errors = suite._test_contract_validation(base)
            # Missing required fields
            assert any("apiVersion" in e or "kind" in e or "spec" in e for e in errors)


# ---------------------------------------------------------------------------
# ForgeTestRunner
# ---------------------------------------------------------------------------


class TestForgeTestRunner:
    @patch("fluid_build.forge.core.testing.get_template_registry")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    def test_init(self, _mock_prov, _mock_tmpl):
        runner = ForgeTestRunner()
        assert runner.template_registry is not None
        assert runner.provider_registry is not None

    @patch("fluid_build.forge.core.testing.get_template_registry")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    def test_run_all_empty(self, _mock_prov, mock_tmpl):
        mock_tmpl.return_value.list_available.return_value = []
        runner = ForgeTestRunner()
        results = runner.run_all_template_tests()
        assert results == {}

    @patch("fluid_build.forge.core.testing.cprint")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    def test_run_all_with_template(self, _mock_prov, mock_tmpl, _mock_cprint):
        mock_tmpl.return_value.list_available.return_value = ["test_tmpl"]
        # The test suite will fail because template not found
        mock_tmpl.return_value.get.return_value = None

        runner = ForgeTestRunner()
        results = runner.run_all_template_tests()
        assert "test_tmpl" in results

    def test_generate_test_report(self):
        runner = ForgeTestRunner.__new__(ForgeTestRunner)
        results = {
            "tmpl_a": TestResult(
                success=True,
                errors=[],
                warnings=["w1"],
                generated_files=["f1"],
                execution_time=0.5,
            ),
            "tmpl_b": TestResult(
                success=False,
                errors=["e1"],
                warnings=[],
                generated_files=[],
                execution_time=1.0,
            ),
        }
        report = runner.generate_test_report(results)
        assert "tmpl_a" in report
        assert "tmpl_b" in report
        assert "PASS" in report
        assert "FAIL" in report
        assert "50.0%" in report


# ---------------------------------------------------------------------------
# test_all_templates
# ---------------------------------------------------------------------------


class TestAllTemplatesFunc:
    @patch("fluid_build.forge.core.testing.cprint")
    @patch("fluid_build.forge.core.testing.ForgeTestRunner")
    def test_test_all_templates(self, mock_runner_cls, _mock_cprint):
        from fluid_build.forge.core.testing import test_all_templates

        # Mock runner to return one result to avoid division-by-zero in source
        mock_result = MagicMock()
        mock_result.success = True
        mock_runner_cls.return_value.run_all_template_tests.return_value = {"t1": mock_result}
        results = test_all_templates()
        assert isinstance(results, dict)
        assert len(results) == 1
