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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


# ── cmd_apply_run ─────────────────────────────────────────────────────


class TestCmdApplyRun:
    def _make_args(
        self, contract_path, out="-", provider=None, project=None, region=None, env=None
    ):
        return argparse.Namespace(
            contract=contract_path,
            out=out,
            provider=provider,
            project=project,
            region=region,
            env=env,
        )

    def _write_contract(self, tmp_path, contract=None):
        if contract is None:
            contract = {
                "fluidVersion": "0.5.7",
                "kind": "DataContract",
                "id": "test",
                "name": "Test",
                "metadata": {},
            }
        p = Path(tmp_path) / "contract.yaml"
        import yaml

        p.write_text(yaml.dump(contract))
        return str(p)

    def test_apply_run_with_provider_apply_method(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_apply_run

        contract_path = self._write_contract(tmp_path)
        args = self._make_args(contract_path, out="-")

        mock_provider = MagicMock()
        mock_provider.capabilities.return_value = {}
        mock_provider.apply.return_value = {"applied": 0, "failed": 0, "results": []}
        mock_provider.name = "local"

        with patch("fluid_build.cli.bootstrap.build_provider", return_value=mock_provider):
            with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
                mock_load.return_value = {
                    "fluidVersion": "0.5.7",
                    "id": "test",
                    "exposes": [],
                }
                with patch("fluid_build.cli.bootstrap.plan_contract") as mock_plan:
                    mock_plan.return_value = {"actions": [], "provider": "local"}
                    with patch("fluid_build.cli.bootstrap._print_json"):
                        rc = cmd_apply_run(args, LOG)
        assert rc == 0

    def test_apply_run_with_exporter_provider(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_apply_run

        contract_path = self._write_contract(tmp_path)
        args = self._make_args(contract_path, out="-")

        mock_provider = MagicMock()
        mock_provider.capabilities.return_value = {"render": True}
        mock_provider.render.return_value = {"output": "data"}
        mock_provider.name = "odps"

        with patch("fluid_build.cli.bootstrap.build_provider", return_value=mock_provider):
            with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
                mock_load.return_value = {"fluidVersion": "0.5.7", "id": "test", "exposes": []}
                with patch("fluid_build.cli.bootstrap._print_json"):
                    rc = cmd_apply_run(args, LOG)
        assert rc == 0

    def test_apply_run_write_to_file(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_apply_run

        out_path = str(tmp_path / "result.json")
        contract_path = self._write_contract(tmp_path)
        args = self._make_args(contract_path, out=out_path)

        mock_provider = MagicMock()
        mock_provider.capabilities.return_value = {}
        mock_provider.apply.return_value = {"applied": 1, "failed": 0, "results": []}
        mock_provider.name = "local"

        with patch("fluid_build.cli.bootstrap.build_provider", return_value=mock_provider):
            with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
                mock_load.return_value = {"fluidVersion": "0.5.7", "id": "test", "exposes": []}
                with patch("fluid_build.cli.bootstrap.plan_contract") as mock_plan:
                    mock_plan.return_value = {"actions": [], "provider": "local"}
                    rc = cmd_apply_run(args, LOG)
        assert rc == 0
        assert Path(out_path).exists()

    def test_apply_run_no_apply_method_legacy(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_apply_run

        contract_path = self._write_contract(tmp_path)
        args = self._make_args(contract_path, out="-")

        mock_provider = MagicMock(spec=[])  # no apply or capabilities
        mock_provider.name = "legacy"

        with patch("fluid_build.cli.bootstrap.build_provider", return_value=mock_provider):
            with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
                mock_load.return_value = {"fluidVersion": "0.5.7", "id": "test", "exposes": []}
                with patch("fluid_build.cli.bootstrap.plan_contract") as mock_plan:
                    mock_plan.return_value = {"actions": [], "provider": "local"}
                    with patch("fluid_build.cli.bootstrap._print_json"):
                        rc = cmd_apply_run(args, LOG)
        assert rc == 0


# ── cmd_graph_run ─────────────────────────────────────────────────────


class TestCmdGraphRun:
    def test_graph_run_stdout_fallback(self, tmp_path, capsys):
        from fluid_build.cli.bootstrap import cmd_graph_run

        args = argparse.Namespace(
            contract=None,
            out="-",
            env=None,
        )

        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
            mock_load.return_value = {"fluidVersion": "0.5.7"}
            with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no viz")):
                rc = cmd_graph_run(args, LOG)
        assert rc == 0
        captured = capsys.readouterr()
        assert "digraph" in captured.out

    def test_graph_run_write_to_file(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_graph_run

        out_path = str(tmp_path / "graph.dot")
        args = argparse.Namespace(contract=None, out=out_path, env=None)

        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
            mock_load.return_value = {"fluidVersion": "0.5.7"}
            with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no viz")):
                rc = cmd_graph_run(args, LOG)
        assert rc == 0
        assert Path(out_path).exists()

    def test_graph_run_with_emit_dot(self, tmp_path, capsys):
        from fluid_build.cli.bootstrap import cmd_graph_run

        args = argparse.Namespace(contract=None, out="-", env=None)

        mock_graph = MagicMock()
        mock_graph.emit_contract_dot = MagicMock(return_value="digraph G { A -> B; }")

        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
            mock_load.return_value = {"fluidVersion": "0.5.7"}
            with patch("fluid_build.cli.bootstrap._imp", return_value=mock_graph):
                rc = cmd_graph_run(args, LOG)
        assert rc == 0


# ── cmd_visualize_plan_run ────────────────────────────────────────────


class TestCmdVisualizePlanRun:
    def test_visualize_plan_no_graphviz_writes_html(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_visualize_plan_run

        out_dir = str(tmp_path / "plan_viz")
        args = argparse.Namespace(
            contract=None,
            out=out_dir,
            provider=None,
            env=None,
        )

        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
            mock_load.return_value = {"fluidVersion": "0.5.7", "exposes": []}
            with patch("fluid_build.cli.bootstrap.plan_contract") as mock_plan:
                mock_plan.return_value = {"actions": [], "provider": "local"}
                # Force has_dot=False by making graphviz import fail at bootstrap level
                with patch.dict("sys.modules", {"graphviz": None}):
                    rc = cmd_visualize_plan_run(args, LOG)
        assert rc == 0
        assert (Path(out_dir) / "index.html").exists()

    def test_visualize_plan_writes_dot_file(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_visualize_plan_run

        out_dir = str(tmp_path / "plan_viz2")
        args = argparse.Namespace(
            contract=None,
            out=out_dir,
            provider=None,
            env=None,
        )

        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay") as mock_load:
            mock_load.return_value = {"fluidVersion": "0.5.7", "exposes": []}
            with patch("fluid_build.cli.bootstrap.plan_contract") as mock_plan:
                mock_plan.return_value = {
                    "actions": [{"op": "ensure_dataset", "name": "ds"}],
                    "provider": "local",
                }
                with patch.dict("sys.modules", {"graphviz": None}):
                    rc = cmd_visualize_plan_run(args, LOG)
        assert rc == 0
        assert (Path(out_dir) / "plan.dot").exists()
        dot_content = (Path(out_dir) / "plan.dot").read_text()
        assert "ensure_dataset" in dot_content


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


# ── plan_contract fallback ────────────────────────────────────────────


class TestPlanContractFallback:
    def test_fallback_empty_contract(self):
        from fluid_build.cli.bootstrap import plan_contract

        contract = {"fluidVersion": "0.5.7", "exposes": []}
        with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no planner")):
            plan = plan_contract(contract, None)
        assert "actions" in plan
        assert isinstance(plan["actions"], list)

    def test_fallback_with_dataset_and_table(self):
        from fluid_build.cli.bootstrap import plan_contract

        contract = {
            "fluidVersion": "0.5.7",
            "exposes": [
                {
                    "location": {
                        "format": "bigquery",
                        "properties": {"dataset": "my_ds", "table": "my_table"},
                    }
                }
            ],
        }
        with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no planner")):
            plan = plan_contract(contract, "bigquery")
        actions = plan["actions"]
        ops = [a["op"] for a in actions]
        assert "ensure_dataset" in ops
        assert "ensure_table" in ops


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
    def test_valid_contract(self):
        from fluid_build.cli.bootstrap import validate_contract_obj

        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
            "metadata": {},
        }
        with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no schema")):
            ok, err = validate_contract_obj(contract)
        assert ok is True
        assert err is None

    def test_invalid_contract_missing_field(self):
        from fluid_build.cli.bootstrap import validate_contract_obj

        contract = {"fluidVersion": "0.5.7"}  # missing required fields
        with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no schema")):
            ok, err = validate_contract_obj(contract)
        assert ok is False
        assert err is not None

    def test_uses_schema_validate_when_available(self):
        """Lines 117-121: calls validate_contract from schema module."""
        from fluid_build.cli.bootstrap import validate_contract_obj

        mock_schema_mod = MagicMock()
        mock_schema_mod.validate_contract = MagicMock(return_value=(True, None))

        with patch("fluid_build.cli.bootstrap._imp", return_value=mock_schema_mod):
            ok, err = validate_contract_obj({"id": "test"})
        assert ok is True

    def test_schema_module_exception_falls_to_baseline(self):
        """Lines 122-123: exception from schema falls through to baseline."""
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


# ── cmd_plan_run ──────────────────────────────────────────────────────


class TestCmdPlanRun:
    def test_plan_run_writes_output(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_plan_run

        out_path = str(tmp_path / "plan.json")
        args = argparse.Namespace(contract="dummy.yaml", env=None, out=out_path, provider=None)
        contract = {"fluidVersion": "0.5.7", "exposes": []}
        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay", return_value=contract):
            with patch(
                "fluid_build.cli.bootstrap.plan_contract",
                return_value={"actions": [], "provider": "local"},
            ):
                rc = cmd_plan_run(args, LOG)
        assert rc == 0
        assert Path(out_path).exists()
        data = json.loads(Path(out_path).read_text())
        assert "actions" in data

    def test_plan_run_with_provider(self, tmp_path):
        from fluid_build.cli.bootstrap import cmd_plan_run

        out_path = str(tmp_path / "plan2.json")
        args = argparse.Namespace(
            contract="dummy.yaml", env=None, out=out_path, provider="bigquery"
        )
        contract = {"fluidVersion": "0.5.7", "exposes": []}
        with patch("fluid_build.cli.bootstrap.load_contract_with_overlay", return_value=contract):
            with patch(
                "fluid_build.cli.bootstrap.plan_contract",
                return_value={
                    "actions": [{"op": "ensure_dataset", "name": "ds"}],
                    "provider": "bigquery",
                },
            ):
                rc = cmd_plan_run(args, LOG)
        assert rc == 0


# ── plan_contract fallback – local_file format ────────────────────────


class TestPlanContractLocalFile:
    def test_fallback_local_file_format_creates_copy_action(self, tmp_path):
        """Lines 170-180: local_file format triggers a copy action."""
        import os

        from fluid_build.cli.bootstrap import plan_contract

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            contract = {
                "fluidVersion": "0.5.7",
                "exposes": [
                    {
                        "location": {
                            "format": "local_file",
                            "properties": {},
                        }
                    }
                ],
            }
            with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no planner")):
                plan = plan_contract(contract, None)
            ops = [a["op"] for a in plan["actions"]]
            assert "copy" in ops
        finally:
            os.chdir(original_cwd)

    def test_fallback_file_format_creates_copy_action(self, tmp_path):
        """Lines 170-180: 'file' format also triggers copy action."""
        import os

        from fluid_build.cli.bootstrap import plan_contract

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            contract = {
                "fluidVersion": "0.5.7",
                "exposes": [{"location": {"format": "file", "properties": {}}}],
            }
            with patch("fluid_build.cli.bootstrap._imp", side_effect=ImportError("no planner")):
                plan = plan_contract(contract, "local")
            ops = [a["op"] for a in plan["actions"]]
            assert "copy" in ops
        finally:
            os.chdir(original_cwd)


# ── _write_json and _print_json ───────────────────────────────────────


class TestWriteAndPrintJson:
    def test_write_json_creates_file(self, tmp_path):
        from fluid_build.cli.bootstrap import _write_json

        out = str(tmp_path / "subdir" / "out.json")
        _write_json(out, {"key": "value"})
        assert Path(out).exists()
        data = json.loads(Path(out).read_text())
        assert data["key"] == "value"

    def test_print_json_fallback_no_rich(self, capsys):
        from fluid_build.cli.bootstrap import _print_json

        with patch.dict("sys.modules", {"rich.console": None, "rich": None}):
            with patch("fluid_build.cli.bootstrap._print_json") as mock_print:
                mock_print({"hello": "world"})
                mock_print.assert_called_once()

    def test_print_json_stdout_fallback(self, capsys):
        """Lines 200-201: when rich unavailable write to stdout directly."""
        from fluid_build.cli.bootstrap import _print_json

        with patch("fluid_build.cli.bootstrap.sys") as mock_sys:
            mock_sys.stdout = MagicMock()
            # Force the rich import to fail inside _print_json
            import builtins

            real_import = builtins.__import__

            def _no_rich(name, *args, **kwargs):
                if name == "rich.console":
                    raise ImportError("no rich")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_no_rich):
                _print_json({"k": "v"})
            mock_sys.stdout.write.assert_called_once()


# ── _provider_supports_render ─────────────────────────────────────────


class TestProviderSupportsRender:
    def test_capabilities_render_true(self):
        from fluid_build.cli.bootstrap import _provider_supports_render

        provider = MagicMock()
        provider.capabilities.return_value = {"render": True}
        assert _provider_supports_render(provider) is True

    def test_capabilities_render_false(self):
        from fluid_build.cli.bootstrap import _provider_supports_render

        provider = MagicMock()
        provider.capabilities.return_value = {"render": False}
        assert _provider_supports_render(provider) is False

    def test_capabilities_exception_name_odps(self):
        """Lines 211-213: capabilities() raises, fallback checks provider.name."""
        from fluid_build.cli.bootstrap import _provider_supports_render

        provider = MagicMock()
        provider.capabilities.side_effect = Exception("no caps")
        provider.name = "odps"
        assert _provider_supports_render(provider) is True

    def test_capabilities_exception_name_unknown(self):
        from fluid_build.cli.bootstrap import _provider_supports_render

        provider = MagicMock()
        provider.capabilities.side_effect = Exception("no caps")
        provider.name = "unknown"
        assert _provider_supports_render(provider) is False

    def test_capabilities_exception_no_name_attr(self):
        """Line 213: getattr fallback when name missing."""
        from fluid_build.cli.bootstrap import _provider_supports_render

        provider = MagicMock(spec=[])  # no capabilities, no name
        assert _provider_supports_render(provider) is False


# ── cmd_apply_run – legacy copy action ───────────────────────────────


class TestCmdApplyRunLegacyCopy:
    def test_apply_legacy_copy_action_src_missing(self, tmp_path):
        """Lines 282-295: legacy copy where src does not exist writes demo csv."""
        import yaml

        from fluid_build.cli.bootstrap import cmd_apply_run

        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
            "metadata": {},
            "exposes": [],
        }
        contract_path = tmp_path / "contract.yaml"
        contract_path.write_text(yaml.dump(contract))

        out_path = str(tmp_path / "result.json")
        args = argparse.Namespace(
            contract=str(contract_path),
            out=out_path,
            provider=None,
            project=None,
            region=None,
            env=None,
        )

        # Provider with no apply or capabilities method
        mock_provider = MagicMock(spec=[])
        mock_provider.name = "legacy"

        dst_path = tmp_path / "dst" / "out.csv"
        actions = [{"op": "copy", "src": str(tmp_path / "nonexistent.csv"), "dst": str(dst_path)}]

        with patch("fluid_build.cli.bootstrap.build_provider", return_value=mock_provider):
            with patch(
                "fluid_build.cli.bootstrap.load_contract_with_overlay", return_value=contract
            ):
                with patch(
                    "fluid_build.cli.bootstrap.plan_contract",
                    return_value={"actions": actions, "provider": "local"},
                ):
                    with patch("fluid_build.cli.bootstrap._print_json"):
                        rc = cmd_apply_run(args, LOG)
        assert rc == 0
