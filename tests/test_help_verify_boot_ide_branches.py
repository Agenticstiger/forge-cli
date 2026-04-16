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

"""Branch-coverage tests for help_formatter, verify, bootstrap, and ide modules."""

import argparse
import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ===========================================================================
# help_formatter.py
# ===========================================================================


class TestHelpFormatterConstants:
    def test_command_enrichment_exists(self):
        from fluid_build.cli.help_formatter import _COMMAND_ENRICHMENT

        assert isinstance(_COMMAND_ENRICHMENT, dict)
        assert len(_COMMAND_ENRICHMENT) > 10
        # Each value is (description, epilog) tuple
        for key, val in _COMMAND_ENRICHMENT.items():
            assert isinstance(key, str)
            assert isinstance(val, tuple)
            assert len(val) == 2

    def test_enrichment_has_core_commands(self):
        from fluid_build.cli.help_formatter import _COMMAND_ENRICHMENT

        for cmd in ("init", "apply", "validate", "plan", "verify", "test", "auth", "doctor"):
            assert cmd in _COMMAND_ENRICHMENT

    def test_rich_available_is_bool(self):
        from fluid_build.cli.help_formatter import RICH_AVAILABLE

        assert isinstance(RICH_AVAILABLE, bool)


class TestPrintFirstRunHelp:
    def test_no_rich(self):
        from fluid_build.cli import help_formatter

        parser = MagicMock()
        with patch.object(help_formatter, "RICH_AVAILABLE", False):
            help_formatter.print_first_run_help(parser)
        parser.print_help.assert_called_once()

    def test_with_rich(self):
        from fluid_build.cli import help_formatter

        if not help_formatter.RICH_AVAILABLE:
            pytest.skip("Rich not available")
        parser = MagicMock()
        help_formatter.print_first_run_help(parser)  # Should not raise


class TestPrintMainHelp:
    def test_no_rich(self):
        from fluid_build.cli import help_formatter

        parser = MagicMock()
        with patch.object(help_formatter, "RICH_AVAILABLE", False):
            help_formatter.print_main_help(parser)
        parser.print_help.assert_called_once()

    def test_with_rich(self):
        from fluid_build.cli import help_formatter

        if not help_formatter.RICH_AVAILABLE:
            pytest.skip("Rich not available")
        parser = MagicMock()
        help_formatter.print_main_help(parser)  # Should not raise


class TestPrintForgeHelp:
    def test_no_rich(self):
        from fluid_build.cli import help_formatter

        with patch.object(help_formatter, "RICH_AVAILABLE", False):
            result = help_formatter.print_forge_help()
        assert result is False

    def test_with_rich(self):
        from fluid_build.cli import help_formatter

        if not help_formatter.RICH_AVAILABLE:
            pytest.skip("Rich not available")
        result = help_formatter.print_forge_help()
        assert result is True


# ===========================================================================
# verify.py
# ===========================================================================


class TestAssessDriftSeverity:
    def test_critical_missing_fields(self):
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity(["col1"], [], [], [], True)
        assert result["level"] == "CRITICAL"
        assert result["impact"] == "HIGH"
        assert "missing fields" in result["actions"][0].lower()

    def test_critical_type_mismatches(self):
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity([], [], ["int vs str"], [], True)
        assert result["level"] == "CRITICAL"

    def test_critical_region_mismatch(self):
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity([], [], [], [], False)
        assert result["level"] == "CRITICAL"
        assert any("region" in a.lower() for a in result["actions"])

    def test_critical_multiple(self):
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity(["c1"], [], ["t1"], [], False)
        assert result["level"] == "CRITICAL"
        assert len(result["actions"]) == 3

    def test_warning_mode_mismatches(self):
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity([], [], [], ["nullable -> required"], True)
        assert result["level"] == "WARNING"
        assert result["impact"] == "MEDIUM"
        assert result["remediation"] == "MANUAL_RECOMMENDED"

    def test_info_extra_fields(self):
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity([], ["extra_col"], [], [], True)
        assert result["level"] == "INFO"
        assert result["impact"] == "LOW"
        assert result["remediation"] == "AUTO_FIXABLE"

    def test_success_all_clear(self):
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity([], [], [], [], True)
        assert result["level"] == "SUCCESS"
        assert result["impact"] == "NONE"
        assert result["actions"] == []

    def test_critical_before_warning(self):
        # Missing fields + mode mismatch -> critical (takes priority)
        from fluid_build.cli.verify import assess_drift_severity

        result = assess_drift_severity(["c1"], [], [], ["m1"], True)
        assert result["level"] == "CRITICAL"


class TestVerifyRegister:
    def test_register(self):
        from fluid_build.cli.verify import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)


class TestVerifyCommand:
    def test_command_name(self):
        from fluid_build.cli.verify import COMMAND

        assert COMMAND == "verify"


# ===========================================================================
# bootstrap.py
# ===========================================================================


class TestBootstrapActiveProfile:
    def test_default_experimental(self):
        from fluid_build.cli.bootstrap import _active_profile

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLUID_BUILD_PROFILE", None)
            assert _active_profile() == "experimental"

    def test_custom_profile(self):
        from fluid_build.cli.bootstrap import _active_profile

        with patch.dict(os.environ, {"FLUID_BUILD_PROFILE": "stable"}):
            assert _active_profile() == "stable"

    def test_case_insensitive(self):
        from fluid_build.cli.bootstrap import _active_profile

        with patch.dict(os.environ, {"FLUID_BUILD_PROFILE": "STABLE"}):
            assert _active_profile() == "stable"


class TestIsCommandEnabled:
    def test_experimental_all_on(self):
        from fluid_build.cli.bootstrap import is_command_enabled

        with patch.dict(os.environ, {"FLUID_BUILD_PROFILE": "experimental"}):
            assert is_command_enabled("init") is True
            assert is_command_enabled("random_unknown_command") is True

    def test_stable_known(self):
        from fluid_build.cli.bootstrap import is_command_enabled

        with patch.dict(os.environ, {"FLUID_BUILD_PROFILE": "stable"}):
            assert is_command_enabled("init") is True
            assert is_command_enabled("validate") is True
            assert is_command_enabled("apply") is True

    def test_stable_unknown(self):
        from fluid_build.cli.bootstrap import is_command_enabled

        with patch.dict(os.environ, {"FLUID_BUILD_PROFILE": "stable"}):
            assert is_command_enabled("marketplace") is False
            assert is_command_enabled("copilot") is False


class TestBootstrapStableCommands:
    def test_stable_commands_frozenset(self):
        from fluid_build.cli.bootstrap import _STABLE_COMMANDS

        assert isinstance(_STABLE_COMMANDS, frozenset)
        assert "init" in _STABLE_COMMANDS
        assert "validate" in _STABLE_COMMANDS
        assert "apply" in _STABLE_COMMANDS


class TestBootstrapImp:
    def test_import_module(self):
        from fluid_build.cli.bootstrap import _imp

        result = _imp("json")
        assert hasattr(result, "dumps")

    def test_import_attribute(self):
        from fluid_build.cli.bootstrap import _imp

        result = _imp("os.path", "join")
        assert callable(result)

    def test_import_nonexistent(self):
        from fluid_build.cli.bootstrap import _imp

        with pytest.raises(ModuleNotFoundError):
            _imp("nonexistent_module_xyz_99")


class TestValidateContractObj:
    @patch("fluid_build.cli.bootstrap._imp")
    def test_valid_via_schema(self, mock_imp):
        from fluid_build.cli.bootstrap import validate_contract_obj

        mock_mod = MagicMock()
        mock_mod.validate_contract.return_value = (True, None)
        mock_imp.return_value = mock_mod
        ok, err = validate_contract_obj({"id": "test"})
        assert ok is True

    @patch("fluid_build.cli.bootstrap._imp")
    def test_invalid_via_schema(self, mock_imp):
        from fluid_build.cli.bootstrap import validate_contract_obj

        mock_mod = MagicMock()
        mock_mod.validate_contract.return_value = (False, "schema error")
        mock_imp.return_value = mock_mod
        ok, err = validate_contract_obj({"id": "test"})
        assert ok is False
        assert err == "schema error"

    @patch("fluid_build.cli.bootstrap._imp")
    def test_fallback_valid(self, mock_imp):
        from fluid_build.cli.bootstrap import validate_contract_obj

        mock_mod = MagicMock(spec=[])  # no validate_contract attribute
        mock_imp.return_value = mock_mod
        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
            "metadata": {},
        }
        ok, err = validate_contract_obj(contract)
        assert ok is True

    def test_missing_field(self):
        from fluid_build.cli.bootstrap import validate_contract_obj

        contract = {"fluidVersion": "0.5.7", "kind": "DataContract"}
        ok, err = validate_contract_obj(contract)
        assert ok is False
        assert "id" in err

    def test_totally_empty(self):
        from fluid_build.cli.bootstrap import validate_contract_obj

        ok, err = validate_contract_obj({})
        assert ok is False


class TestPlanContract:
    def test_fallback_empty(self):
        from fluid_build.cli.bootstrap import plan_contract

        contract = {"exposes": []}
        plan = plan_contract(contract, "local")
        assert "actions" in plan
        assert plan["provider"] == "local"

    def test_fallback_with_dataset_table(self):
        from fluid_build.cli.bootstrap import plan_contract

        contract = {
            "exposes": [
                {
                    "location": {
                        "format": "bigquery",
                        "properties": {"dataset": "ds1", "table": "t1"},
                    },
                    "schema": [{"name": "col1"}],
                }
            ]
        }
        plan = plan_contract(contract, "gcp")
        actions = plan["actions"]
        ops = [a["op"] for a in actions]
        assert "ensure_dataset" in ops
        assert "ensure_table" in ops

    def test_fallback_file_format(self, tmp_path):
        from fluid_build.cli.bootstrap import plan_contract

        contract = {"exposes": [{"location": {"format": "file", "properties": {}}}]}
        plan = plan_contract(contract, None)
        actions = plan["actions"]
        ops = [a["op"] for a in actions]
        assert "copy" in ops
        assert plan["provider"] == "unknown"

    def test_fallback_no_exposes(self):
        from fluid_build.cli.bootstrap import plan_contract

        plan = plan_contract({}, None)
        assert plan["actions"] == []


class TestBootstrapWriteJson:
    def test_write_json(self, tmp_path):
        from fluid_build.cli.bootstrap import _write_json

        path = str(tmp_path / "sub" / "out.json")
        _write_json(path, {"key": "val"})
        data = json.loads(Path(path).read_text())
        assert data == {"key": "val"}


class TestBootstrapPrintJson:
    def test_print_json(self, capsys):
        from fluid_build.cli.bootstrap import _print_json

        _print_json({"a": 1})
        # Should produce output without error


class TestProviderSupportsRender:
    def test_supports_render_true(self):
        from fluid_build.cli.bootstrap import _provider_supports_render

        p = MagicMock()
        p.capabilities.return_value = {"render": True}
        assert _provider_supports_render(p) is True

    def test_supports_render_false(self):
        from fluid_build.cli.bootstrap import _provider_supports_render

        p = MagicMock()
        p.capabilities.return_value = {}
        assert _provider_supports_render(p) is False

    def test_supports_render_exception_fallback(self):
        from fluid_build.cli.bootstrap import _provider_supports_render

        p = MagicMock()
        p.capabilities.side_effect = RuntimeError("no caps")
        p.name = "odps"
        assert _provider_supports_render(p) is True

    def test_supports_render_exception_fallback_other(self):
        from fluid_build.cli.bootstrap import _provider_supports_render

        p = MagicMock()
        p.capabilities.side_effect = RuntimeError("no caps")
        p.name = "gcp"
        assert _provider_supports_render(p) is False


class TestCmdValidateRun:
    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay")
    @patch("fluid_build.cli.bootstrap.validate_contract_obj")
    def test_valid(self, mock_validate, mock_load):
        from fluid_build.cli.bootstrap import cmd_validate_run

        mock_load.return_value = {"id": "test"}
        mock_validate.return_value = (True, None)
        args = argparse.Namespace(contract="c.yaml", env=None)
        assert cmd_validate_run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay")
    @patch("fluid_build.cli.bootstrap.validate_contract_obj")
    def test_invalid(self, mock_validate, mock_load):
        from fluid_build.cli.bootstrap import cmd_validate_run

        mock_load.return_value = {"id": "test"}
        mock_validate.return_value = (False, "bad field")
        args = argparse.Namespace(contract="c.yaml", env=None)
        assert cmd_validate_run(args, logging.getLogger()) == 2


class TestCmdPlanRun:
    @patch("fluid_build.cli.bootstrap._write_json")
    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay")
    def test_plan(self, mock_load, mock_write):
        from fluid_build.cli.bootstrap import cmd_plan_run

        mock_load.return_value = {"exposes": []}
        args = argparse.Namespace(contract="c.yaml", env=None, provider=None, out="/tmp/plan.json")
        assert cmd_plan_run(args, logging.getLogger()) == 0
        mock_write.assert_called_once()


# ===========================================================================
# ide.py
# ===========================================================================


class TestIDEEnums:
    def test_ide_types(self):
        from fluid_build.cli.ide import IDEType

        vals = [e.value for e in IDEType]
        assert "vscode" in vals
        assert "intellij" in vals
        assert "vim" in vals

    def test_completion_types(self):
        from fluid_build.cli.ide import CompletionType

        vals = [e.value for e in CompletionType]
        assert "command" in vals
        assert "provider" in vals
        assert "field_name" in vals


class TestCompletionItem:
    def test_create(self):
        from fluid_build.cli.ide import CompletionItem, CompletionType

        ci = CompletionItem(
            label="validate",
            kind=CompletionType.COMMAND,
            detail="Validate",
            documentation="Validate contract",
            insert_text="validate ${1:file}",
        )
        assert ci.label == "validate"
        assert ci.score == 1.0

    def test_to_dict(self):
        from fluid_build.cli.ide import CompletionItem, CompletionType

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


class TestDiagnosticItem:
    def test_create(self):
        from fluid_build.cli.ide import DiagnosticItem

        di = DiagnosticItem(
            file_path="/tmp/test.yaml", line=10, column=5, severity="error", message="Bad syntax"
        )
        assert di.source == "fluid"
        assert di.code is None

    def test_to_dict(self):
        from fluid_build.cli.ide import DiagnosticItem

        di = DiagnosticItem(
            file_path="/tmp/test.yaml",
            line=10,
            column=5,
            severity="warning",
            message="Missing field",
            source="fluid",
            code="W001",
        )
        d = di.to_dict()
        assert d["file"] == "/tmp/test.yaml"
        assert d["line"] == 10
        assert d["severity"] == "warning"
        assert d["code"] == "W001"


class TestFluidLanguageServer:
    def test_init(self):
        from fluid_build.cli.ide import FluidLanguageServer

        ls = FluidLanguageServer()
        assert ls.workspace_root is None
        assert isinstance(ls.open_files, dict)
        assert isinstance(ls.completions_cache, dict)

    def test_completion_data_loaded(self):
        from fluid_build.cli.ide import FluidLanguageServer

        ls = FluidLanguageServer()
        assert "commands" in ls.completion_data
        assert "providers" in ls.completion_data
        assert len(ls.completion_data["commands"]) > 0
        assert len(ls.completion_data["providers"]) > 0

    def test_completion_data_items_are_completion_items(self):
        from fluid_build.cli.ide import CompletionItem, FluidLanguageServer

        ls = FluidLanguageServer()
        for item in ls.completion_data["commands"]:
            assert isinstance(item, CompletionItem)

    def test_initialize(self, tmp_path):
        from fluid_build.cli.ide import FluidLanguageServer

        ls = FluidLanguageServer()
        ls.initialize(str(tmp_path))
        assert ls.workspace_root == Path(str(tmp_path))


class TestIDECommand:
    def test_command_name(self):
        from fluid_build.cli.ide import COMMAND

        assert COMMAND == "ide"
