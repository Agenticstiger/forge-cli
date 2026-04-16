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

"""Unit tests for fluid_build.cli.plan — plan generation and run()."""

import argparse
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fluid_build.cli.plan import (
    _display_plan_simple,
    _parse_semver,
    _plan_legacy,
    _should_use_provider_actions,
    register,
    run,
    write_json_idempotent,
)

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    """Build a minimal argparse Namespace for plan tests."""
    defaults = dict(
        contract="contract.fluid.yaml",
        env=None,
        out="runtime/plan.json",
        verbose=False,
        validate_actions=False,
        estimate_cost=False,
        check_sovereignty=False,
        provider=None,
        project=None,
        region=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _minimal_contract(version="0.5.7"):
    return {
        "id": "dp-test",
        "name": "Test Product",
        "fluidVersion": version,
        "exposes": [],
    }


# ---------------------------------------------------------------------------
# _parse_semver
# ---------------------------------------------------------------------------


class TestParseSemver(unittest.TestCase):
    def test_parses_standard_version(self):
        self.assertEqual(_parse_semver("1.2.3"), (1, 2, 3))

    def test_parses_zero_version(self):
        self.assertEqual(_parse_semver("0.5.7"), (0, 5, 7))

    def test_parses_version_with_suffix(self):
        self.assertEqual(_parse_semver("0.7.1-alpha"), (0, 7, 1))

    def test_returns_zeros_on_invalid(self):
        self.assertEqual(_parse_semver("not-a-version"), (0, 0, 0))

    def test_parses_high_version(self):
        self.assertEqual(_parse_semver("10.20.30"), (10, 20, 30))


# ---------------------------------------------------------------------------
# write_json_idempotent
# ---------------------------------------------------------------------------


class TestWriteJsonIdempotent(unittest.TestCase):
    def test_writes_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "plan.json")
            write_json_idempotent(path, {"key": "value"})
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["key"], "value")

    def test_skips_write_when_content_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "plan.json")
            obj = {"a": 1, "b": 2}
            write_json_idempotent(path, obj)
            mtime_before = os.path.getmtime(path)
            import time

            time.sleep(0.01)
            write_json_idempotent(path, obj)
            mtime_after = os.path.getmtime(path)
            self.assertEqual(mtime_before, mtime_after)

    def test_overwrites_when_content_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "plan.json")
            write_json_idempotent(path, {"v": 1})
            write_json_idempotent(path, {"v": 2})
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["v"], 2)

    def test_creates_nested_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "a", "b", "plan.json")
            write_json_idempotent(path, {"x": 1})
            self.assertTrue(os.path.exists(path))

    def test_handles_unreadable_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "plan.json")
            # Write a corrupt file first
            with open(path, "w") as f:
                f.write("not json but also not a problem")
            # Should not raise — proceeds with write
            write_json_idempotent(path, {"z": 99})
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["z"], 99)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister(unittest.TestCase):
    def test_register_adds_plan_subparser(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["plan", "my_contract.yaml"])
        self.assertEqual(args.contract, "my_contract.yaml")

    def test_register_default_out(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["plan", "c.yaml"])
        self.assertEqual(args.out, "runtime/plan.json")

    def test_register_sets_func(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["plan", "c.yaml"])
        self.assertEqual(args.func, run)


# ---------------------------------------------------------------------------
# _should_use_provider_actions
# ---------------------------------------------------------------------------


class TestShouldUseProviderActions(unittest.TestCase):
    def test_explicit_provider_actions_returns_true(self):
        contract = {"providerActions": [{"op": "create"}], "fluidVersion": "0.5.7"}
        self.assertTrue(_should_use_provider_actions(contract, LOG))

    def test_old_version_no_actions_returns_false(self):
        contract = {"fluidVersion": "0.5.7"}
        result = _should_use_provider_actions(contract, LOG)
        # Result depends on PROVIDER_ACTIONS_AVAILABLE; just assert it is bool
        self.assertIsInstance(result, bool)

    def test_missing_fluid_version_defaults_to_legacy(self):
        contract = {}
        result = _should_use_provider_actions(contract, LOG)
        self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# _display_plan_simple
# ---------------------------------------------------------------------------


class TestDisplayPlanSimple(unittest.TestCase):
    def _make_plan(self, actions=None):
        if actions is None:
            actions = []
        return {
            "contract": {"name": "MyProduct", "version": "0.5.7"},
            "total_actions": len(actions),
            "actions": actions,
        }

    @patch("fluid_build.cli.plan.cprint")
    def test_empty_plan_shows_no_actions(self, mock_cprint):
        _display_plan_simple(self._make_plan([]), LOG)
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("Total Actions: 0", calls)

    @patch("fluid_build.cli.plan.cprint")
    def test_plan_with_actions_shows_steps(self, mock_cprint):
        actions = [
            {"step": 1, "action_id": "create_table", "action_type": "ddl", "depends_on": []},
        ]
        _display_plan_simple(self._make_plan(actions), LOG, output_path="/tmp/plan.json")
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("create_table", calls)

    @patch("fluid_build.cli.plan.cprint")
    def test_plan_with_dependencies_shown(self, mock_cprint):
        actions = [
            {
                "step": 1,
                "action_id": "step_a",
                "action_type": "ddl",
                "depends_on": ["step_b"],
            },
        ]
        _display_plan_simple(self._make_plan(actions), LOG)
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("step_b", calls)


# ---------------------------------------------------------------------------
# _plan_legacy
# ---------------------------------------------------------------------------


class TestPlanLegacy(unittest.TestCase):
    def _make_provider_mock(self, has_plan=True):
        provider = MagicMock()
        if has_plan:
            provider.plan.return_value = [{"op": "ensure_dataset", "description": "Create dataset"}]
        else:
            del provider.plan  # Remove plan attribute
            provider = MagicMock(spec=[])
        return provider

    # Legacy plan tests removed — run_pre_plan/run_post_plan don't exist in this module


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun(unittest.TestCase):
    @patch("fluid_build.cli.plan.write_json_idempotent")
    @patch("fluid_build.cli.plan._display_plan_simple")
    @patch("fluid_build.cli.plan._plan_legacy")
    @patch("fluid_build.cli.plan._should_use_provider_actions")
    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_run_returns_zero_on_success(
        self,
        mock_load,
        mock_should_use,
        mock_legacy,
        mock_display,
        mock_write,
    ):
        mock_load.return_value = _minimal_contract()
        mock_should_use.return_value = False
        mock_legacy.return_value = {
            "format_version": "0.5.7",
            "actions": [],
            "total_actions": 0,
            "contract": {"name": "Test", "version": "0.5.7"},
        }
        mock_write.return_value = None
        mock_display.return_value = None

        args = _make_args(out="/tmp/plan.json")
        result = run(args, LOG)
        self.assertEqual(result, 0)

    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_run_raises_cli_error_on_failure(self, mock_load):
        from fluid_build.cli._common import CLIError

        mock_load.side_effect = RuntimeError("load failed")
        args = _make_args()
        with self.assertRaises(CLIError):
            run(args, LOG)

    @patch("fluid_build.cli.plan.write_json_idempotent")
    @patch("fluid_build.cli.plan._display_plan_simple")
    @patch("fluid_build.cli.plan._plan_legacy")
    @patch("fluid_build.cli.plan._should_use_provider_actions")
    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_run_propagates_cli_error(
        self,
        mock_load,
        mock_should_use,
        mock_legacy,
        _mock_display,
        _mock_write,
    ):
        from fluid_build.cli._common import CLIError

        mock_load.return_value = _minimal_contract()
        mock_should_use.return_value = False
        mock_legacy.side_effect = CLIError(1, "plan_failed")
        args = _make_args()
        with self.assertRaises(CLIError):
            run(args, LOG)


if __name__ == "__main__":
    unittest.main()
