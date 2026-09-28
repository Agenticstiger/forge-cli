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

"""Extended tests for fluid_build.cli.bootstrap covering previously uncovered lines."""

import argparse
import json
import logging
import tempfile
from unittest.mock import MagicMock, patch

LOG = logging.getLogger(__name__)


# ── load_contract_with_overlay ────────────────────────────────────────


class TestLoadContractWithOverlay:
    def test_loads_yaml_contract(self):
        import yaml

        from fluid_build.cli._common import load_contract_with_overlay

        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            yaml.dump(contract, f)
            path = f.name

        result = load_contract_with_overlay(path, None, LOG)
        assert result["id"] == "test"

    def test_loads_json_contract(self):
        from fluid_build.cli._common import load_contract_with_overlay

        contract = {"fluidVersion": "0.5.7", "id": "json-test"}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(contract, f)
            path = f.name

        result = load_contract_with_overlay(path, None, LOG)
        assert result["id"] == "json-test"


# ── register_core_commands ────────────────────────────────────────────


class TestRegisterCoreCommands:
    def test_register_core_commands_stable_profile(self):
        from fluid_build.cli.bootstrap import register_core_commands

        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "stable"}):
            # Patch out imports that might fail
            with patch(
                "fluid_build.cli.bootstrap.importlib.import_module",
                side_effect=ImportError("mocked"),
            ):
                register_core_commands(sp)

        # Fallback parsers should have been registered for stable commands
        assert sp.choices, "expected at least one subparser registered"

    def test_register_core_commands_experimental_profile(self):
        from fluid_build.cli.bootstrap import register_core_commands

        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "experimental"}):
            with patch(
                "fluid_build.cli.bootstrap.importlib.import_module",
                side_effect=ImportError("mocked"),
            ):
                register_core_commands(sp)

        assert sp.choices, "expected at least one subparser registered"

    def test_register_core_commands_adds_validate_fallback(self):
        from fluid_build.cli import bootstrap as bootstrap_mod
        from fluid_build.cli.bootstrap import register_core_commands

        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")

        # Save the real importlib reference, then replace with a mock
        # that fails on import_module. Using patch.object avoids the
        # nested-patch issue where patching importlib.import_module globally
        # breaks the patch() machinery itself.
        fake_importlib = MagicMock()
        fake_importlib.import_module.side_effect = ImportError("no module")

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "stable"}):
            with patch.object(bootstrap_mod, "importlib", fake_importlib):
                register_core_commands(sp)

        assert sp.choices, "expected at least one subparser registered"
        assert "validate" in sp.choices, "expected validate fallback parser"


# ── _try_register helper ──────────────────────────────────────────────


class TestTryRegister:
    def test_try_register_import_error_returns_false(self):
        from fluid_build.cli.bootstrap import _try_register

        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")

        with patch(
            "fluid_build.cli.bootstrap.importlib.import_module",
            side_effect=ImportError("no such module"),
        ):
            result = _try_register(sp, "nonexistent_module", "nonexistent-cmd")
        assert result is False

    def test_try_register_profile_disabled_returns_false(self):
        from fluid_build.cli.bootstrap import _try_register

        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "stable"}):
            result = _try_register(sp, "copilot", "copilot")
        assert result is False


# ── get_reporter ──────────────────────────────────────────────────────


class TestGetReporter:
    def test_get_reporter_returns_none_on_error(self):
        """Lines 93-98: reporter initialization failure falls back to None."""
        from fluid_build.cli import bootstrap as bs

        original = bs._REPORTER
        bs._REPORTER = None
        try:
            with patch(
                "fluid_build.cli.bootstrap._imp",
                side_effect=Exception("observability unavailable"),
            ):
                # Patch at the package level to avoid real import
                with patch.dict(
                    "sys.modules",
                    {"fluid_build.observability": None},
                ):
                    reporter = bs.get_reporter()
            # Should be None when initialization fails
            assert reporter is None
        finally:
            bs._REPORTER = original

    def test_get_reporter_returns_cached(self):
        """Lines 80-99: second call returns cached _REPORTER value."""
        from fluid_build.cli import bootstrap as bs

        original = bs._REPORTER
        sentinel = object()
        bs._REPORTER = sentinel
        try:
            reporter = bs.get_reporter()
            assert reporter is sentinel
        finally:
            bs._REPORTER = original


# ── _imp ─────────────────────────────────────────────────────────────


class TestImp:
    def test_imp_returns_module(self):
        from fluid_build.cli.bootstrap import _imp

        mod = _imp("os")
        import os

        assert mod is os

    def test_imp_returns_attr(self):
        from fluid_build.cli.bootstrap import _imp

        path_cls = _imp("os.path", "join")
        import os

        assert path_cls is os.path.join

    def test_imp_raises_on_missing_module(self):
        import pytest

        from fluid_build.cli.bootstrap import _imp

        with pytest.raises(ModuleNotFoundError):
            _imp("nonexistent_module_xyz_abc_123")


# ── validate_contract_obj ─────────────────────────────────────────────


class TestValidateContractObj:
    def test_valid_contract_baseline_fallback(self):
        """Schema manager unavailable -> minimal required-field check passes."""
        from fluid_build.cli.bootstrap import validate_contract_obj

        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
            "metadata": {},
        }
        with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no schema")):
            ok, err = validate_contract_obj(contract)
        assert ok is True
        assert err is None

    def test_invalid_contract_missing_field_baseline_fallback(self):
        from fluid_build.cli.bootstrap import validate_contract_obj

        contract = {"fluidVersion": "0.7.3"}  # missing required fields
        with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no schema")):
            ok, err = validate_contract_obj(contract)
        assert ok is False
        assert err is not None

    def test_uses_schema_manager_when_available(self):
        """Routes validation through the jsonschema-backed FluidSchemaManager."""
        from fluid_build.cli.bootstrap import validate_contract_obj

        mock_result = MagicMock()
        mock_result.is_valid = True
        mock_manager = MagicMock()
        mock_manager.validate_contract = MagicMock(return_value=mock_result)
        mock_schema_mgr_mod = MagicMock()
        mock_schema_mgr_mod.FluidSchemaManager = MagicMock(return_value=mock_manager)

        with patch("fluid_build.cli.bootstrap._imp", return_value=mock_schema_mgr_mod):
            ok, err = validate_contract_obj({"id": "test"})
        assert ok is True

    def test_schema_manager_exception_falls_to_baseline(self):
        """Exception from the schema manager falls through to baseline."""
        from fluid_build.cli.bootstrap import validate_contract_obj

        with patch("fluid_build.cli.bootstrap._imp", side_effect=Exception("schema broken")):
            ok, err = validate_contract_obj(
                {
                    "fluidVersion": "x",
                    "kind": "DataContract",
                    "id": "t",
                    "name": "T",
                    "metadata": {},
                }
            )
        assert ok is True


# ── cmd_validate_run ──────────────────────────────────────────────────


class TestCmdValidateRun:
    def test_validate_success(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_validate_run

        args = argparse.Namespace(contract="dummy.yaml", env=None)
        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "t",
            "name": "T",
            "metadata": {},
        }
        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay", return_value=contract):
            with patch(
                "fluid_build.cli.bootstrap.validate_contract_obj", return_value=(True, None)
            ):
                rc = cmd_validate_run(args, LOG)
        assert rc == 0

    def test_validate_failure(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_validate_run

        args = argparse.Namespace(contract="dummy.yaml", env=None)
        contract = {"fluidVersion": "0.5.7"}  # invalid
        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay", return_value=contract):
            with patch(
                "fluid_build.cli.bootstrap.validate_contract_obj",
                return_value=(False, "missing field"),
            ):
                with patch("fluid_build.cli.bootstrap.console_error"):
                    rc = cmd_validate_run(args, LOG)
        assert rc == 2
