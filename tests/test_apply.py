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

"""Unit tests for fluid_build.cli.apply module."""

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import _actions_from_source, register, run

logger = logging.getLogger("test_apply")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    defaults = {
        "contract": "contract.fluid.yaml",
        "env": None,
        "yes": True,
        "dry_run": False,
        "timeout": 120,
        "parallel_phases": False,
        "max_workers": 4,
        "rollback_strategy": "phase_complete",
        "require_approval": False,
        "backup_state": False,
        "validate_dependencies": False,
        "report": None,
        "report_format": "html",
        "metrics_export": "none",
        "notify": None,
        "verbose": False,
        "debug": False,
        "keep_temp_files": False,
        "profile": False,
        "workspace_dir": Path("."),
        "state_file": None,
        "config_override": None,
        "provider_config": None,
    }
    defaults.update(kwargs)
    args = Mock()
    for k, v in defaults.items():
        setattr(args, k, v)
    return args


def _make_provider():
    provider = Mock()
    provider.apply.return_value = {"failed": 0, "applied": 2, "status": "success"}
    return provider


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_adds_apply_command(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["apply", "contract.fluid.yaml"])
        assert args.contract == "contract.fluid.yaml"

    def test_register_dry_run_flag(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["apply", "contract.fluid.yaml", "--dry-run"])
        assert args.dry_run is True

    def test_register_env_flag(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["apply", "contract.fluid.yaml", "--env", "prod"])
        assert args.env == "prod"

    def test_register_rollback_strategy(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(
            ["apply", "contract.fluid.yaml", "--rollback-strategy", "immediate"]
        )
        assert args.rollback_strategy == "immediate"


# ---------------------------------------------------------------------------
# _actions_from_source
# ---------------------------------------------------------------------------


class TestActionsFromSource:
    def test_json_source_reads_actions(self, tmp_path):
        plan = {"actions": [{"op": "ensure_dataset"}, {"op": "ensure_table"}]}
        json_file = tmp_path / "plan.json"
        json_file.write_text(json.dumps(plan))
        provider = _make_provider()

        with patch("fluid_build.cli.apply.read_json", return_value=plan):
            actions = _actions_from_source(str(json_file), None, provider, logger)

        assert len(actions) == 2
        assert actions[0]["op"] == "ensure_dataset"

    def test_yaml_source_uses_provider_plan(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        provider = Mock()
        provider.plan.return_value = [{"op": "s3.ensure_bucket"}]
        contract = {"id": "test"}

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            actions = _actions_from_source(str(contract_file), None, provider, logger)

        assert len(actions) == 1
        assert actions[0]["op"] == "s3.ensure_bucket"

    def test_yaml_source_fallback_to_parser(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\n")
        # A spec-less mock with no plan() method
        provider = Mock(spec=[])
        contract = {"id": "test"}

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            actions = _actions_from_source(str(contract_file), None, provider, logger)

        assert isinstance(actions, list)

    def test_json_source_empty_actions(self, tmp_path):
        plan = {"actions": []}
        json_file = tmp_path / "empty_plan.json"
        json_file.write_text(json.dumps(plan))
        provider = _make_provider()

        with patch("fluid_build.cli.apply.read_json", return_value=plan):
            actions = _actions_from_source(str(json_file), None, provider, logger)

        assert actions == []

    def test_provider_plan_exception_falls_back(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\n")
        provider = Mock()
        provider.plan.side_effect = Exception("plan failed")
        contract = {"id": "test"}

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            actions = _actions_from_source(str(contract_file), None, provider, logger)

        assert isinstance(actions, list)


# ---------------------------------------------------------------------------
# run() - simple mode (hooks are imported locally inside run())
# ---------------------------------------------------------------------------


def _patch_hooks():
    """Return patch context for the three hooks imported inside apply.run()."""
    return (
        patch("fluid_build.cli.hooks.run_pre_apply", new=Mock(side_effect=lambda p, a, l: a)),
        patch("fluid_build.cli.hooks.run_post_apply", new=Mock()),
        patch("fluid_build.cli.hooks.run_on_error", new=Mock()),
    )


class TestRunSimpleMode:
    def _minimal_contract(self):
        return {"id": "test-product", "name": "Test Product"}

    def test_run_dry_run_returns_0(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        args = _make_args(contract=str(contract_file), dry_run=True, yes=True)
        contract = self._minimal_contract()

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=_make_provider()):
                with patch(
                    "fluid_build.cli.apply._actions_from_source",
                    return_value=[{"op": "ensure_dataset"}],
                ):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        result = run(args, logger)
        assert result == 0

    def test_run_no_actions_returns_0(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        args = _make_args(contract=str(contract_file), yes=True)
        contract = self._minimal_contract()

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=_make_provider()):
                with patch("fluid_build.cli.apply._actions_from_source", return_value=[]):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        result = run(args, logger)
        assert result == 0

    def test_run_success_returns_0(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        args = _make_args(contract=str(contract_file), yes=True)
        contract = self._minimal_contract()
        actions = [{"op": "ensure_dataset"}]

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=_make_provider()):
                with patch("fluid_build.cli.apply._actions_from_source", return_value=actions):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        pre, post, on_err = _patch_hooks()
                        with pre, post, on_err:
                            result = run(args, logger)
        assert result == 0

    def test_run_failure_returns_1(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        args = _make_args(contract=str(contract_file), yes=True)
        contract = self._minimal_contract()
        actions = [{"op": "ensure_dataset"}]
        fail_provider = Mock()
        fail_provider.apply.side_effect = RuntimeError("boom")

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=fail_provider):
                with patch("fluid_build.cli.apply._actions_from_source", return_value=actions):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        pre, post, on_err = _patch_hooks()
                        with pre, post, on_err:
                            try:
                                result = run(args, logger)
                            except CLIError as exc:
                                # Provider error wrapped in CLIError — success
                                assert exc.exit_code == 1
                            except RuntimeError:
                                # Provider error re-raised directly — also acceptable
                                pass
                            else:
                                # run() returned instead of raising — must be non-zero
                                assert result != 0, (
                                    "run() should signal failure via CLIError, "
                                    "RuntimeError, or non-zero return code"
                                )

    def test_run_with_config_override(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        override = json.dumps({"region": "us-east-1"})
        args = _make_args(contract=str(contract_file), yes=True, config_override=override)
        contract = self._minimal_contract()

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=_make_provider()):
                with patch("fluid_build.cli.apply._actions_from_source", return_value=[]):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        result = run(args, logger)
        assert result == 0

    def test_run_with_invalid_config_override_raises_cli_error(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        args = _make_args(contract=str(contract_file), yes=True, config_override="{invalid-json")
        contract = self._minimal_contract()

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider") as mock_build_provider:
                with pytest.raises(CLIError) as excinfo:
                    run(args, logger)

        assert excinfo.value.exit_code == 2
        assert excinfo.value.event == "invalid_config_override"
        assert excinfo.value.message == "Invalid --config-override JSON"
        mock_build_provider.assert_not_called()

    def test_run_with_gcp_provider_detection(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\n")
        args = _make_args(contract=str(contract_file), yes=True)
        contract = {
            "id": "test",
            "exposes": [
                {
                    "binding": {
                        "platform": "gcp",
                        "location": {"project": "my-project"},
                    }
                }
            ],
        }

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=_make_provider()):
                with patch("fluid_build.cli.apply._actions_from_source", return_value=[]):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        result = run(args, logger)
        assert result == 0

    def test_run_with_aws_provider_detection(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\n")
        args = _make_args(contract=str(contract_file), yes=True)
        contract = {
            "id": "test",
            "exposes": [
                {
                    "binding": {
                        "platform": "aws",
                        "location": {"region": "us-east-1"},
                    }
                }
            ],
        }

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=_make_provider()):
                with patch("fluid_build.cli.apply._actions_from_source", return_value=[]):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        result = run(args, logger)
        assert result == 0

    def test_run_report_json_generated(self, tmp_path):
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text("id: test\nname: Test\n")
        report_file = tmp_path / "report.json"
        args = _make_args(
            contract=str(contract_file),
            yes=True,
            report=str(report_file),
            report_format="json",
        )
        contract = self._minimal_contract()
        actions = [{"op": "ensure_dataset"}]

        with patch("fluid_build.cli.apply.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.apply.build_provider", return_value=_make_provider()):
                with patch("fluid_build.cli.apply._actions_from_source", return_value=actions):
                    with patch("fluid_build.cli.apply.RICH_AVAILABLE", False):
                        pre, post, on_err = _patch_hooks()
                        with pre, post, on_err:
                            result = run(args, logger)
        assert result == 0
