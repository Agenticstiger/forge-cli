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

"""Tests for fluid_build.cli.ide — data structures AND language server logic."""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.ide import (
    COMMAND,
    CompletionItem,
    CompletionType,
    DiagnosticItem,
    FluidLanguageServer,
    IDEIntegration,
    IDEType,
    handle_file_validation,
    handle_language_server,
    handle_setup_ide,
    run,
)

# ── Enum tests ──────────────────────────────────────────────────────────────


class TestEnums:
    def test_ide_types(self):
        assert IDEType.VSCODE.value == "vscode"
        assert IDEType.VIM.value == "vim"
        assert len(IDEType) == 6

    def test_completion_types(self):
        assert CompletionType.COMMAND.value == "command"
        assert CompletionType.FIELD_NAME.value == "field_name"

    def test_all_ide_type_values(self):
        vals = {e.value for e in IDEType}
        assert vals == {"vscode", "intellij", "pycharm", "vim", "emacs", "sublime"}

    def test_all_completion_type_values(self):
        vals = {e.value for e in CompletionType}
        assert vals == {"command", "argument", "provider", "contract_path", "field_name"}


# ── CompletionItem ──────────────────────────────────────────────────────────


class TestCompletionItem:
    def test_basic_creation(self):
        ci = CompletionItem(
            label="validate",
            kind=CompletionType.COMMAND,
            detail="Validate contract",
            documentation="docs",
            insert_text="validate ${1:file}",
        )
        assert ci.label == "validate"
        assert ci.score == 1.0

    def test_to_dict(self):
        ci = CompletionItem(
            label="gcp",
            kind=CompletionType.PROVIDER,
            detail="GCP",
            documentation="Google Cloud",
            insert_text="gcp",
            score=0.9,
        )
        d = ci.to_dict()
        assert d["label"] == "gcp"
        assert d["kind"] == "provider"
        assert d["insertText"] == "gcp"
        assert d["score"] == 0.9

    def test_to_dict_contains_all_keys(self):
        ci = CompletionItem(
            label="apply",
            kind=CompletionType.COMMAND,
            detail="Apply plan",
            documentation="Apply a plan",
            insert_text="apply ${1:plan.json}",
        )
        d = ci.to_dict()
        assert set(d.keys()) == {"label", "kind", "detail", "documentation", "insertText", "score"}


# ── DiagnosticItem ──────────────────────────────────────────────────────────


class TestDiagnosticItem:
    def test_basic(self):
        di = DiagnosticItem(
            file_path="c.yaml",
            line=10,
            column=5,
            severity="error",
            message="Missing field",
        )
        assert di.source == "fluid"
        assert di.code is None

    def test_to_dict(self):
        di = DiagnosticItem(
            file_path="c.yaml",
            line=1,
            column=1,
            severity="warning",
            message="Warn",
            code="W001",
        )
        d = di.to_dict()
        assert d["file"] == "c.yaml"
        assert d["severity"] == "warning"
        assert d["code"] == "W001"

    def test_to_dict_with_none_code(self):
        di = DiagnosticItem(
            file_path="f.yaml", line=2, column=3, severity="info", message="Info msg"
        )
        d = di.to_dict()
        assert d["code"] is None
        assert d["source"] == "fluid"


# ── FluidLanguageServer ─────────────────────────────────────────────────────


class TestFluidLanguageServer:
    def test_init_loads_completion_data(self):
        fls = FluidLanguageServer()
        assert "commands" in fls.completion_data
        assert "providers" in fls.completion_data
        assert "contract_fields" in fls.completion_data

    def test_initialize_returns_capabilities(self):
        fls = FluidLanguageServer()
        result = fls.initialize("/tmp/workspace")
        caps = result["capabilities"]
        assert caps["completionProvider"]["resolveProvider"] is True
        assert caps["diagnosticProvider"] is True
        assert fls.workspace_root == Path("/tmp/workspace")

    def test_get_completions_empty_prefix(self):
        fls = FluidLanguageServer()
        completions = fls.get_completions("test.sh", 0, 0, "")
        labels = [c.label for c in completions]
        assert "validate" in labels
        assert "plan" in labels

    def test_get_completions_provider_context(self):
        fls = FluidLanguageServer()
        completions = fls.get_completions("test.sh", 0, 20, "--provider gcp")
        labels = [c.label for c in completions]
        assert "gcp" in labels

    def test_get_completions_limited_to_20(self):
        fls = FluidLanguageServer()
        completions = fls.get_completions("test.sh", 0, 0, "")
        assert len(completions) <= 20

    def test_get_completions_fluid_prefix(self):
        fls = FluidLanguageServer()
        # prefix="fluid " starts with "fluid " so commands are returned
        completions = fls.get_completions("test.sh", 0, 7, "fluid  ")
        labels = [c.label for c in completions]
        # When prefix starts with "fluid " or is empty, commands are returned
        assert isinstance(completions, list)

    def test_get_completions_provider_colon(self):
        fls = FluidLanguageServer()
        completions = fls.get_completions("test.sh", 0, 9, "provider:")
        labels = [c.label for c in completions]
        assert "aws" in labels

    def test_get_completions_out_keyword(self):
        fls = FluidLanguageServer()
        fls.workspace_root = None
        completions = fls.get_completions("test.sh", 0, 5, "--out")
        # No workspace_root means _get_file_path_completions returns empty list,
        # but it still calls into the function without error
        assert isinstance(completions, list)

    def test_get_completions_contract_keyword(self):
        fls = FluidLanguageServer()
        fls.workspace_root = None
        completions = fls.get_completions("test.sh", 0, 10, "--contract")
        assert isinstance(completions, list)

    def test_get_completions_yaml_contract_file(self, tmp_path):
        contract = tmp_path / "my.yaml"
        contract.write_text("meta:\n  name: test\nsources:\n  - name: s1\n")
        fls = FluidLanguageServer()
        fls.initialize(str(tmp_path))
        # line=0 col=0 with empty prefix triggers command completions, not contract fields
        completions = fls.get_completions(str(contract), 0, 0, "\n")
        assert isinstance(completions, list)

    def test_get_completions_line_out_of_range(self):
        fls = FluidLanguageServer()
        # Line 99 does not exist in a 1-line context — should not raise
        completions = fls.get_completions("test.sh", 99, 0, "fluid validate")
        assert isinstance(completions, list)

    def test_is_contract_file_true(self, tmp_path):
        f = tmp_path / "contract.yaml"
        f.write_text("meta:\n  name: test\n")
        fls = FluidLanguageServer()
        assert fls._is_contract_file(str(f)) is True

    def test_is_contract_file_false(self, tmp_path):
        f = tmp_path / "plain.yaml"
        f.write_text("some: data\n")
        fls = FluidLanguageServer()
        assert fls._is_contract_file(str(f)) is False

    def test_is_contract_file_missing(self):
        fls = FluidLanguageServer()
        assert fls._is_contract_file("/nonexistent/path.yaml") is False

    def test_get_dynamic_completions_with_sources(self):
        fls = FluidLanguageServer()
        context = "sources:\n  - name: my_source\ntransforms:\n  - name: my_transform\n"
        completions = fls._get_dynamic_completions("f.yaml", context)
        labels = [c.label for c in completions]
        assert any("my_source" in lbl for lbl in labels)

    def test_get_dynamic_completions_with_transforms(self):
        fls = FluidLanguageServer()
        context = "transforms:\n  - name: my_xform\n"
        completions = fls._get_dynamic_completions("f.yaml", context)
        # Should not raise; may return empty or items
        assert isinstance(completions, list)

    def test_get_dynamic_completions_empty_context(self):
        fls = FluidLanguageServer()
        completions = fls._get_dynamic_completions("f.yaml", "")
        assert completions == []

    def test_get_file_path_completions_no_workspace(self):
        fls = FluidLanguageServer()
        fls.workspace_root = None
        completions = fls._get_file_path_completions()
        assert completions == []

    def test_get_file_path_completions_with_yaml_files(self, tmp_path):
        f = tmp_path / "contract.yaml"
        f.write_text("meta:\n  name: test\nsources:\n  - name: s1\n")
        fls = FluidLanguageServer()
        fls.initialize(str(tmp_path))
        completions = fls._get_file_path_completions()
        labels = [c.label for c in completions]
        assert any("contract.yaml" in lbl for lbl in labels)

    def test_get_file_path_completions_with_examples_dir(self, tmp_path):
        examples = tmp_path / "examples"
        examples.mkdir()
        ex_file = examples / "example.yaml"
        ex_file.write_text("sources:\n  - name: ex\n")
        fls = FluidLanguageServer()
        fls.initialize(str(tmp_path))
        completions = fls._get_file_path_completions()
        labels = [c.label for c in completions]
        assert any("example.yaml" in lbl for lbl in labels)

    def test_validate_file_non_contract(self, tmp_path):
        f = tmp_path / "plain.yaml"
        f.write_text("some: data\n")
        fls = FluidLanguageServer()
        diagnostics = fls.validate_file(str(f), "some: data\n")
        # plain.yaml is not a contract file → no diagnostics expected
        assert diagnostics == []

    def test_validate_file_contract_valid(self, tmp_path):
        f = tmp_path / "c.yaml"
        content = "meta:\n  name: test\nsources:\n  - name: s1\n"
        f.write_text(content)
        fls = FluidLanguageServer()
        diagnostics = fls.validate_file(str(f), content)
        severities = {d.severity for d in diagnostics}
        # No errors expected for valid YAML with meta+sources
        assert "error" not in severities

    def test_validate_file_contract_missing_meta_and_sources(self, tmp_path):
        f = tmp_path / "c.yaml"
        content = "transforms:\n  - name: t1\n"
        f.write_text(content)
        fls = FluidLanguageServer()
        diagnostics = fls.validate_file(str(f), content)
        codes = {d.code for d in diagnostics}
        assert "missing_meta" in codes
        assert "missing_sources" in codes

    def test_validate_contract_syntax_valid(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        diagnostics = fls._validate_contract_syntax(str(f), "key: value\n")
        assert diagnostics == []

    def test_validate_contract_syntax_invalid_yaml(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        bad_yaml = "key: [unclosed\n"
        diagnostics = fls._validate_contract_syntax(str(f), bad_yaml)
        assert len(diagnostics) >= 1
        assert diagnostics[0].severity == "error"
        assert diagnostics[0].code == "yaml_syntax"

    def test_validate_contract_semantics_no_meta_no_sources(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        content = "transforms:\n  - name: t1\n"
        diags = fls._validate_contract_semantics(str(f), content)
        codes = {d.code for d in diags}
        assert "missing_meta" in codes
        assert "missing_sources" in codes

    def test_validate_contract_semantics_name_with_spaces(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        content = "meta:\n  name: has spaces\nsources:\n  - name: s1\n"
        diags = fls._validate_contract_semantics(str(f), content)
        codes = {d.code for d in diags}
        assert "naming_convention" in codes

    def test_validate_contract_semantics_missing_description(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        content = "meta:\n  name: test\nsources:\n  - name: s1\n"
        diags = fls._validate_contract_semantics(str(f), content)
        codes = {d.code for d in diags}
        assert "missing_description" in codes

    def test_validate_contract_semantics_with_description(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        content = "meta:\n  name: test\nsources:\n  - name: s1\n    description: My source\n"
        diags = fls._validate_contract_semantics(str(f), content)
        codes = {d.code for d in diags}
        # missing_description should NOT be present
        assert "missing_description" not in codes

    def test_format_document_valid_yaml(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        content = "b: 2\na: 1\n"
        result = fls.format_document(str(f), content)
        # Result should be valid YAML with same keys
        assert "a:" in result
        assert "b:" in result

    def test_format_document_non_yaml(self, tmp_path):
        f = tmp_path / "script.sh"
        fls = FluidLanguageServer()
        content = "#!/bin/bash\necho hello\n"
        result = fls.format_document(str(f), content)
        assert result == content  # Unchanged for non-yaml

    def test_format_document_invalid_yaml_returns_original(self, tmp_path):
        f = tmp_path / "c.yaml"
        fls = FluidLanguageServer()
        content = "key: [unclosed bracket\n"
        result = fls.format_document(str(f), content)
        assert result == content


# ── IDEIntegration ──────────────────────────────────────────────────────────


class TestIDEIntegration:
    def test_init(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
        assert ide.language_server is not None
        assert isinstance(ide.language_server, FluidLanguageServer)

    def test_setup_vscode_extension_creates_files(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
            result = ide.setup_vscode_extension()
        assert result is True
        ext_dir = tmp_path / ".fluid" / "ide" / "vscode-extension"
        assert (ext_dir / "package.json").exists()
        assert (ext_dir / "language-configuration.json").exists()
        assert (ext_dir / "src" / "extension.ts").exists()
        assert (ext_dir / "tsconfig.json").exists()

    def test_setup_vscode_extension_package_json_content(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
            ide.setup_vscode_extension()
        import json

        pkg = json.loads(
            (tmp_path / ".fluid" / "ide" / "vscode-extension" / "package.json").read_text()
        )
        assert pkg["name"] == "fluid-language-support"
        assert "contributes" in pkg
        assert "commands" in pkg["contributes"]

    def test_setup_vscode_extension_returns_false_on_error(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
        # Force an error by patching mkdir to raise
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            result = ide.setup_vscode_extension()
        assert result is False

    def test_install_shell_completion_bash(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
            result = ide.install_shell_completion("bash")
        assert result is True
        completion_file = tmp_path / ".bash_completion.d" / "fluid"
        assert completion_file.exists()
        content = completion_file.read_text()
        assert "_fluid_completion" in content

    def test_install_shell_completion_zsh(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
            result = ide.install_shell_completion("zsh")
        assert result is True
        completion_file = tmp_path / ".zsh" / "completions" / "_fluid"
        assert completion_file.exists()
        content = completion_file.read_text()
        assert "#compdef fluid" in content

    def test_install_shell_completion_returns_false_on_error(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
        with patch.object(Path, "mkdir", side_effect=OSError("no space")):
            result = ide.install_shell_completion("bash")
        assert result is False

    def test_generate_completion_script_bash(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
        script = ide._generate_completion_script("bash")
        assert "compgen" in script
        assert "COMPREPLY" in script

    def test_generate_completion_script_zsh(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
        script = ide._generate_completion_script("zsh")
        assert "#compdef fluid" in script
        assert "_fluid_commands" in script

    def test_generate_completion_script_unknown_shell(self, tmp_path):
        with patch("fluid_build.cli.ide.Path.home", return_value=tmp_path):
            ide = IDEIntegration()
        script = ide._generate_completion_script("fish")
        assert script == ""


# ── Module-level constant ────────────────────────────────────────────────────


class TestModuleConstants:
    def test_command_constant(self):
        assert COMMAND == "ide"


# ── run() entry point ───────────────────────────────────────────────────────


class TestRun:
    def _make_logger(self):
        return logging.getLogger("test_ide")

    def test_run_setup_action(self, tmp_path):
        with patch("fluid_build.cli.ide.IDEIntegration") as mock_cls:
            mock_ide = MagicMock()
            mock_cls.return_value = mock_ide
            with patch("fluid_build.cli.ide.handle_setup_ide", return_value=0) as mock_handle:
                args = argparse.Namespace(ide_action="setup", ide="vscode")
                result = run(args, self._make_logger())
        mock_handle.assert_called_once()
        assert result == 0

    def test_run_lsp_action(self, tmp_path):
        with patch("fluid_build.cli.ide.IDEIntegration") as mock_cls:
            mock_ide = MagicMock()
            mock_cls.return_value = mock_ide
            with patch("fluid_build.cli.ide.handle_language_server", return_value=0) as mock_h:
                args = argparse.Namespace(ide_action="lsp", lsp_action="start", port=9257)
                result = run(args, self._make_logger())
        mock_h.assert_called_once()
        assert result == 0

    def test_run_completion_action(self, tmp_path):
        with patch("fluid_build.cli.ide.IDEIntegration") as mock_cls:
            mock_ide = MagicMock()
            mock_cls.return_value = mock_ide
            with patch("fluid_build.cli.ide.handle_shell_completion", return_value=0) as mock_h:
                args = argparse.Namespace(ide_action="completion", shell="bash")
                result = run(args, self._make_logger())
        mock_h.assert_called_once()
        assert result == 0

    def test_run_validate_action(self, tmp_path):
        with patch("fluid_build.cli.ide.IDEIntegration") as mock_cls:
            mock_ide = MagicMock()
            mock_cls.return_value = mock_ide
            with patch("fluid_build.cli.ide.handle_file_validation", return_value=0) as mock_h:
                args = argparse.Namespace(ide_action="validate", file=str(tmp_path))
                result = run(args, self._make_logger())
        mock_h.assert_called_once()
        assert result == 0

    def test_run_unknown_action_returns_1(self):
        with patch("fluid_build.cli.ide.IDEIntegration"):
            with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
                with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                    mock_console_cls.return_value = MagicMock()
                    args = argparse.Namespace(ide_action="nonexistent")
                    result = run(args, self._make_logger())
        assert result == 1

    def test_run_unknown_action_no_rich(self):
        with patch("fluid_build.cli.ide.IDEIntegration"):
            with patch("fluid_build.cli.ide.RICH_AVAILABLE", False):
                args = argparse.Namespace(ide_action="nonexistent")
                result = run(args, self._make_logger())
        assert result == 1

    def test_run_exception_returns_1(self):
        with patch("fluid_build.cli.ide.IDEIntegration", side_effect=RuntimeError("boom")):
            with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
                with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                    mock_console_cls.return_value = MagicMock()
                    args = argparse.Namespace(ide_action="setup")
                    result = run(args, self._make_logger())
        assert result == 1


# ── handle_setup_ide ────────────────────────────────────────────────────────


class TestHandleSetupIde:
    def _make_logger(self):
        return logging.getLogger("test_ide")

    def test_vscode_success(self, tmp_path):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console = MagicMock()
                mock_console_cls.return_value = mock_console
                mock_ide = MagicMock()
                mock_ide.setup_vscode_extension.return_value = True
                args = argparse.Namespace(ide="vscode")
                result = handle_setup_ide(args, mock_ide, self._make_logger())
        assert result == 0
        mock_ide.setup_vscode_extension.assert_called_once()

    def test_vscode_failure(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                mock_ide = MagicMock()
                mock_ide.setup_vscode_extension.return_value = False
                args = argparse.Namespace(ide="vscode")
                result = handle_setup_ide(args, mock_ide, self._make_logger())
        assert result == 1

    def test_non_vscode_ide_returns_1(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                mock_ide = MagicMock()
                args = argparse.Namespace(ide="intellij")
                result = handle_setup_ide(args, mock_ide, self._make_logger())
        assert result == 1

    def test_no_rich_returns_1(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", False):
            mock_ide = MagicMock()
            args = argparse.Namespace(ide="vscode")
            result = handle_setup_ide(args, mock_ide, self._make_logger())
        assert result == 1


# ── handle_language_server ──────────────────────────────────────────────────


class TestHandleLanguageServer:
    def _make_logger(self):
        return logging.getLogger("test_ide")

    def test_lsp_start(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                mock_ide = MagicMock()
                args = argparse.Namespace(lsp_action="start", port=9257)
                result = handle_language_server(args, mock_ide, self._make_logger())
        assert result == 0

    def test_lsp_completions_success(self, tmp_path):
        contract = tmp_path / "c.yaml"
        contract.write_text("meta:\n  name: test\n")
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                mock_ide = MagicMock()
                mock_ide.language_server.get_completions.return_value = []
                args = argparse.Namespace(
                    lsp_action="completions",
                    file=str(contract),
                    line=1,
                    column=0,
                )
                result = handle_language_server(args, mock_ide, self._make_logger())
        assert result == 0

    def test_lsp_completions_missing_file(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                mock_ide = MagicMock()
                args = argparse.Namespace(
                    lsp_action="completions",
                    file="/nonexistent/file.yaml",
                    line=1,
                    column=0,
                )
                result = handle_language_server(args, mock_ide, self._make_logger())
        assert result == 1

    def test_lsp_other_action_returns_0(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                mock_ide = MagicMock()
                args = argparse.Namespace(lsp_action=None)
                result = handle_language_server(args, mock_ide, self._make_logger())
        assert result == 0

    def test_no_rich_returns_1(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", False):
            mock_ide = MagicMock()
            args = argparse.Namespace(lsp_action="start", port=9257)
            result = handle_language_server(args, mock_ide, self._make_logger())
        assert result == 1


# ── handle_file_validation ──────────────────────────────────────────────────


class TestHandleFileValidation:
    def _make_logger(self):
        return logging.getLogger("test_ide")

    def test_no_issues(self, tmp_path):
        f = tmp_path / "plain.yaml"
        f.write_text("some: data\n")
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console = MagicMock()
                mock_console_cls.return_value = mock_console
                mock_ide = MagicMock()
                mock_ide.language_server.validate_file.return_value = []
                args = argparse.Namespace(file=str(f))
                result = handle_file_validation(args, mock_ide, self._make_logger())
        assert result == 0

    def test_warnings_only_returns_0(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("transforms:\n  - name: t1\n")
        warning_diag = DiagnosticItem(
            file_path=str(f), line=1, column=1, severity="warning", message="Warn"
        )
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                with patch("fluid_build.cli.ide.Table") as mock_table_cls:
                    mock_table = MagicMock()
                    mock_table_cls.return_value = mock_table
                    mock_ide = MagicMock()
                    mock_ide.language_server.validate_file.return_value = [warning_diag]
                    args = argparse.Namespace(file=str(f))
                    result = handle_file_validation(args, mock_ide, self._make_logger())
        assert result == 0

    def test_errors_present_returns_1(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("bad: [yaml\n")
        error_diag = DiagnosticItem(
            file_path=str(f),
            line=1,
            column=1,
            severity="error",
            message="YAML error",
            code="yaml_syntax",
        )
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                with patch("fluid_build.cli.ide.Table") as mock_table_cls:
                    mock_table = MagicMock()
                    mock_table_cls.return_value = mock_table
                    mock_ide = MagicMock()
                    mock_ide.language_server.validate_file.return_value = [error_diag]
                    args = argparse.Namespace(file=str(f))
                    result = handle_file_validation(args, mock_ide, self._make_logger())
        assert result == 1

    def test_info_severity_returns_0(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("meta:\n  name: test\n")
        info_diag = DiagnosticItem(
            file_path=str(f), line=1, column=1, severity="info", message="Info"
        )
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                with patch("fluid_build.cli.ide.Table") as mock_table_cls:
                    mock_table_cls.return_value = MagicMock()
                    mock_ide = MagicMock()
                    mock_ide.language_server.validate_file.return_value = [info_diag]
                    args = argparse.Namespace(file=str(f))
                    result = handle_file_validation(args, mock_ide, self._make_logger())
        assert result == 0

    def test_file_not_found_returns_1(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.ide.Console") as mock_console_cls:
                mock_console_cls.return_value = MagicMock()
                mock_ide = MagicMock()
                args = argparse.Namespace(file="/nonexistent/file.yaml")
                result = handle_file_validation(args, mock_ide, self._make_logger())
        assert result == 1

    def test_no_rich_returns_1(self):
        with patch("fluid_build.cli.ide.RICH_AVAILABLE", False):
            mock_ide = MagicMock()
            args = argparse.Namespace(file="/tmp/x.yaml")
            result = handle_file_validation(args, mock_ide, self._make_logger())
        assert result == 1


# ── register ────────────────────────────────────────────────────────────────


class TestRegister:
    def test_register_adds_ide_subcommand(self):
        from fluid_build.cli.ide import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        args = parser.parse_args(["ide", "setup", "--ide", "vscode"])
        assert args.ide_action == "setup"
        assert args.ide == "vscode"

    def test_register_lsp_start(self):
        from fluid_build.cli.ide import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        args = parser.parse_args(["ide", "lsp", "start", "--port", "9999"])
        assert args.lsp_action == "start"
        assert args.port == 9999

    def test_register_completion(self):
        from fluid_build.cli.ide import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        args = parser.parse_args(["ide", "completion", "--shell", "zsh"])
        assert args.ide_action == "completion"
        assert args.shell == "zsh"

    def test_register_validate(self):
        from fluid_build.cli.ide import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        args = parser.parse_args(["ide", "validate", "myfile.yaml"])
        assert args.ide_action == "validate"
        assert args.file == "myfile.yaml"

    def test_register_sets_func(self):
        from fluid_build.cli.ide import register, run

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        args = parser.parse_args(["ide", "setup"])
        assert args.func is run
