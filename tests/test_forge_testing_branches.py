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

"""Branch-coverage tests for fluid_build.forge.core.testing"""

from unittest.mock import MagicMock, patch

from fluid_build.forge.core.testing import (
    ForgeTestRunner,
    TemplateTestSuite,
    TestResult,
)

# ── TestResult dataclass ────────────────────────────────────────────


class TestTestResult:
    def test_success(self):
        r = TestResult(
            success=True, errors=[], warnings=[], generated_files=["a.py"], execution_time=0.5
        )
        assert r.success is True
        assert r.errors == []
        assert r.generated_files == ["a.py"]

    def test_failure(self):
        r = TestResult(
            success=False, errors=["bad"], warnings=["ok"], generated_files=[], execution_time=1.0
        )
        assert r.success is False
        assert len(r.errors) == 1


# ── TemplateTestSuite._test_metadata ────────────────────────────────


class TestTemplateTestSuiteMetadata:
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_metadata_all_present(self, _mock_tr, _mock_pr):
        suite = TemplateTestSuite("test-tmpl")
        tmpl = MagicMock()
        meta = MagicMock()
        meta.name = "test"
        meta.description = "desc"
        meta.provider_support = ["local"]
        meta.use_cases = ["demo"]
        meta.complexity = "beginner"
        tmpl.get_metadata.return_value = meta
        errors = suite._test_metadata(tmpl)
        assert errors == []

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_metadata_missing_name(self, _mock_tr, _mock_pr):
        suite = TemplateTestSuite("t")
        tmpl = MagicMock()
        meta = MagicMock()
        meta.name = ""
        meta.description = "desc"
        meta.provider_support = ["local"]
        meta.use_cases = ["demo"]
        meta.complexity = "beginner"
        tmpl.get_metadata.return_value = meta
        errors = suite._test_metadata(tmpl)
        assert any("name" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_metadata_missing_description(self, _mock_tr, _mock_pr):
        suite = TemplateTestSuite("t")
        tmpl = MagicMock()
        meta = MagicMock()
        meta.name = "t"
        meta.description = ""
        meta.provider_support = ["local"]
        meta.use_cases = ["demo"]
        meta.complexity = "beginner"
        tmpl.get_metadata.return_value = meta
        errors = suite._test_metadata(tmpl)
        assert any("description" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_metadata_missing_provider_support(self, _mock_tr, _mock_pr):
        suite = TemplateTestSuite("t")
        tmpl = MagicMock()
        meta = MagicMock()
        meta.name = "t"
        meta.description = "d"
        meta.provider_support = []
        meta.use_cases = ["demo"]
        meta.complexity = "beginner"
        tmpl.get_metadata.return_value = meta
        errors = suite._test_metadata(tmpl)
        assert any("provider" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_metadata_missing_use_cases(self, _mock_tr, _mock_pr):
        suite = TemplateTestSuite("t")
        tmpl = MagicMock()
        meta = MagicMock()
        meta.name = "t"
        meta.description = "d"
        meta.provider_support = ["x"]
        meta.use_cases = []
        meta.complexity = "beginner"
        tmpl.get_metadata.return_value = meta
        errors = suite._test_metadata(tmpl)
        assert any("use cases" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_metadata_invalid_complexity(self, _mock_tr, _mock_pr):
        suite = TemplateTestSuite("t")
        tmpl = MagicMock()
        meta = MagicMock()
        meta.name = "t"
        meta.description = "d"
        meta.provider_support = ["x"]
        meta.use_cases = ["x"]
        meta.complexity = "expert"
        tmpl.get_metadata.return_value = meta
        errors = suite._test_metadata(tmpl)
        assert any("complexity" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_metadata_exception(self, _mock_tr, _mock_pr):
        suite = TemplateTestSuite("t")
        tmpl = MagicMock()
        tmpl.get_metadata.side_effect = RuntimeError("boom")
        errors = suite._test_metadata(tmpl)
        assert any("Error" in e for e in errors)


# ── _create_project_structure ────────────────────────────────────────


class TestCreateProjectStructure:
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_dict_content_creates_dir(self, _mock_tr, _mock_pr, tmp_path):
        suite = TemplateTestSuite("t")
        structure = {"subdir": {"file.txt": "hello"}}
        files = suite._create_project_structure(tmp_path, structure)
        assert (tmp_path / "subdir" / "file.txt").exists()
        assert len(files) == 1

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_str_content_creates_file(self, _mock_tr, _mock_pr, tmp_path):
        suite = TemplateTestSuite("t")
        structure = {"readme.md": "# Hello"}
        files = suite._create_project_structure(tmp_path, structure)
        assert (tmp_path / "readme.md").read_text() == "# Hello"
        assert len(files) == 1

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_empty_list_creates_empty_dir(self, _mock_tr, _mock_pr, tmp_path):
        suite = TemplateTestSuite("t")
        structure = {"empty_dir": []}
        files = suite._create_project_structure(tmp_path, structure)
        assert (tmp_path / "empty_dir").is_dir()
        assert files == []

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_empty_dict_creates_dir(self, _mock_tr, _mock_pr, tmp_path):
        suite = TemplateTestSuite("t")
        structure = {"empty": {}}
        files = suite._create_project_structure(tmp_path, structure)
        assert (tmp_path / "empty").is_dir()
        assert files == []


# ── _test_file_validation ────────────────────────────────────────────


class TestFileValidation:
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_missing_required_files(self, _mock_tr, _mock_pr, tmp_path):
        suite = TemplateTestSuite("t")
        errors = suite._test_file_validation(tmp_path)
        assert any("contract.fluid.yaml" in e for e in errors)
        assert any("README.md" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_all_present(self, _mock_tr, _mock_pr, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text(
            "apiVersion: 0.5.7\nkind: DataContract\nmetadata:\n  name: t\nspec:\n  inputs: []"
        )
        (tmp_path / "README.md").write_text("# test")
        (tmp_path / "requirements.txt").write_text("pandas")
        suite = TemplateTestSuite("t")
        errors = suite._test_file_validation(tmp_path)
        assert errors == []

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_bad_python_syntax(self, _mock_tr, _mock_pr, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("apiVersion: 0.5.7")
        (tmp_path / "README.md").write_text("# test")
        (tmp_path / "requirements.txt").write_text("")
        (tmp_path / "bad.py").write_text("def f(\n")
        suite = TemplateTestSuite("t")
        errors = suite._test_file_validation(tmp_path)
        assert any("syntax" in e.lower() for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_bad_yaml(self, _mock_tr, _mock_pr, tmp_path):
        (tmp_path / "README.md").write_text("# test")
        (tmp_path / "requirements.txt").write_text("")
        (tmp_path / "contract.fluid.yaml").write_text(": :\n  bad: [")
        suite = TemplateTestSuite("t")
        errors = suite._test_file_validation(tmp_path)
        # Should have yaml error or just the contract parse issue
        assert len(errors) >= 1


# ── _test_contract_validation ────────────────────────────────────────


class TestContractValidation:
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_no_contract_file(self, _mock_tr, _mock_pr, tmp_path):
        suite = TemplateTestSuite("t")
        errors = suite._test_contract_validation(tmp_path)
        assert any("not found" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_valid_contract(self, _mock_tr, _mock_pr, tmp_path):
        contract = {
            "apiVersion": "0.5.7",
            "kind": "DataContract",
            "metadata": {"name": "test"},
            "spec": {"inputs": [], "outputs": []},
        }
        import yaml as real_yaml

        (tmp_path / "contract.fluid.yaml").write_text(real_yaml.dump(contract))
        suite = TemplateTestSuite("t")
        errors = suite._test_contract_validation(tmp_path)
        assert errors == []

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_missing_required_fields(self, _mock_tr, _mock_pr, tmp_path):
        (tmp_path / "contract.fluid.yaml").write_text("foo: bar\n")
        suite = TemplateTestSuite("t")
        errors = suite._test_contract_validation(tmp_path)
        assert any("apiVersion" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_missing_metadata_name(self, _mock_tr, _mock_pr, tmp_path):
        import yaml as real_yaml

        contract = {"apiVersion": "0.5.7", "kind": "DC", "metadata": {}, "spec": {"inputs": []}}
        (tmp_path / "contract.fluid.yaml").write_text(real_yaml.dump(contract))
        suite = TemplateTestSuite("t")
        errors = suite._test_contract_validation(tmp_path)
        assert any("name" in e for e in errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_no_inputs_or_outputs(self, _mock_tr, _mock_pr, tmp_path):
        import yaml as real_yaml

        contract = {"apiVersion": "0.5.7", "kind": "DC", "metadata": {"name": "t"}, "spec": {}}
        (tmp_path / "contract.fluid.yaml").write_text(real_yaml.dump(contract))
        suite = TemplateTestSuite("t")
        errors = suite._test_contract_validation(tmp_path)
        assert any("inputs or outputs" in e for e in errors)


# ── run_full_test ────────────────────────────────────────────────────


class TestRunFullTest:
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_template_not_found(self, mock_tr, _mock_pr):
        mock_tr.return_value.get.return_value = None
        suite = TemplateTestSuite("missing")
        result = suite.run_full_test({})
        assert result.success is False
        assert any("not found" in e for e in result.errors)

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_unexpected_exception(self, mock_tr, _mock_pr):
        mock_tr.return_value.get.side_effect = RuntimeError("crash")
        suite = TemplateTestSuite("bad")
        result = suite.run_full_test({})
        assert result.success is False
        assert any("Unexpected" in e for e in result.errors)


# ── ForgeTestRunner ─────────────────────────────────────────────────


class TestForgeTestRunner:
    @patch("fluid_build.forge.core.testing.cprint")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_run_all_empty(self, mock_tr, _mock_pr, _mock_cprint):
        mock_tr.return_value.list_available.return_value = []
        runner = ForgeTestRunner()
        results = runner.run_all_template_tests()
        assert results == {}

    @patch("fluid_build.forge.core.testing.cprint")
    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_run_all_with_template(self, mock_tr, _mock_pr, _mock_cprint):
        mock_tr.return_value.list_available.return_value = ["basic"]
        mock_tr.return_value.get.return_value = None  # triggers not found
        runner = ForgeTestRunner()
        results = runner.run_all_template_tests()
        assert "basic" in results
        assert results["basic"].success is False

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_generate_report_all_pass(self, _mock_tr, _mock_pr):
        runner = ForgeTestRunner()
        results = {
            "tmpl1": TestResult(True, [], [], ["a.py"], 0.5),
        }
        report = runner.generate_test_report(results)
        assert "PASS" in report
        assert "tmpl1" in report
        assert "100.0%" in report

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_generate_report_with_failures(self, _mock_tr, _mock_pr):
        runner = ForgeTestRunner()
        results = {
            "tmpl1": TestResult(False, ["err1"], ["warn1"], [], 1.0),
        }
        report = runner.generate_test_report(results)
        assert "FAIL" in report
        assert "err1" in report
        assert "warn1" in report

    @patch("fluid_build.forge.core.testing.get_provider_registry")
    @patch("fluid_build.forge.core.testing.get_template_registry")
    def test_generate_report_mixed(self, _mock_tr, _mock_pr):
        runner = ForgeTestRunner()
        results = {
            "ok": TestResult(True, [], [], ["a"], 0.1),
            "bad": TestResult(False, ["e"], [], [], 0.2),
        }
        report = runner.generate_test_report(results)
        assert "50.0%" in report
