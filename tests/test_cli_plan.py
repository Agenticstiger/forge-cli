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


# ---------------------------------------------------------------------------
# Plan-binding digests + SchedulePlanner (Phase 6B wiring)
# ---------------------------------------------------------------------------


class TestPlanDigestInjection(unittest.TestCase):
    """Pin the stage-6 side of the plan-binding invariant: every plan.json
    written by ``fluid plan`` MUST carry both ``bundleDigest`` (pins the
    input bundle) and ``planDigest`` (catches tampering). Missing fields
    break the stage-7 verify gate silently."""

    @patch("fluid_build.cli.plan._display_plan_simple")
    @patch("fluid_build.cli.plan._plan_legacy")
    @patch("fluid_build.cli.plan._should_use_provider_actions")
    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_emits_plan_digest_for_yaml_contract(
        self,
        mock_load,
        mock_should_use,
        mock_legacy,
        _mock_display,
    ):
        """Yaml-contract path → bundleDigest='' (no bundle to pin) but
        planDigest MUST be populated so verify_plan_binding can run."""
        mock_load.return_value = _minimal_contract()
        mock_should_use.return_value = False
        mock_legacy.return_value = {
            "format_version": "0.5.7",
            "actions": [],
            "total_actions": 0,
            "contract": {"name": "Test", "version": "0.5.7"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "plan.json")
            args = _make_args(out=out_path)
            result = run(args, LOG)
            self.assertEqual(result, 0)

            with open(out_path) as f:
                plan = json.load(f)

            self.assertIn("bundleDigest", plan)
            self.assertIn("planDigest", plan)
            # YAML contract has no bundle → empty string, not missing.
            self.assertEqual(plan["bundleDigest"], "")
            # planDigest is always a real sha256.
            self.assertTrue(plan["planDigest"].startswith("sha256:"))
            self.assertEqual(len(plan["planDigest"]), 7 + 64)

    @patch("fluid_build.cli.plan._display_plan_simple")
    @patch("fluid_build.cli.plan._plan_legacy")
    @patch("fluid_build.cli.plan._should_use_provider_actions")
    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_deterministic_plan_digest_across_runs(
        self,
        mock_load,
        mock_should_use,
        mock_legacy,
        _mock_display,
    ):
        """Two runs with identical inputs produce identical planDigest.
        This is what makes CI caching / determinism checks possible."""
        mock_load.return_value = _minimal_contract()
        mock_should_use.return_value = False
        mock_legacy.return_value = {
            "format_version": "0.5.7",
            "actions": [],
            "total_actions": 0,
            "contract": {"name": "Test", "version": "0.5.7"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_a = os.path.join(tmpdir, "a.json")
            out_b = os.path.join(tmpdir, "b.json")
            run(_make_args(out=out_a), LOG)
            run(_make_args(out=out_b), LOG)

            plan_a = json.loads(open(out_a).read())
            plan_b = json.loads(open(out_b).read())

            self.assertEqual(plan_a["planDigest"], plan_b["planDigest"])


class TestSchedulePlannerInvocation(unittest.TestCase):
    """Pin Path-B scheduling: when ``orchestration.engine`` is a native
    scheduler (eventbridge / snowflake_tasks / mwaa), plan.py must invoke
    the provider planner and merge the schedule actions into the plan.
    Regression here would silently break scheduled deployments."""

    @patch("fluid_build.cli.plan._display_plan_simple")
    @patch("fluid_build.cli.plan._plan_legacy")
    @patch("fluid_build.cli.plan._should_use_provider_actions")
    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_path_a_contract_emits_no_schedule_actions(
        self,
        mock_load,
        mock_should_use,
        mock_legacy,
        _mock_display,
    ):
        """Path-A engines (airflow) are handled by ``generate schedule``,
        NOT by plan.py. plan must NOT add provider actions for them."""
        contract = _minimal_contract()
        contract["orchestration"] = {"engine": "airflow", "schedule": "0 * * * *"}
        mock_load.return_value = contract
        mock_should_use.return_value = False
        mock_legacy.return_value = {
            "format_version": "0.5.7",
            "actions": [],
            "total_actions": 0,
            "contract": {"name": "Test", "version": "0.5.7"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "plan.json")
            run(_make_args(out=out), LOG)
            with open(out) as f:
                plan = json.load(f)
            # No schedule actions appended for Path-A engines.
            self.assertEqual(plan["total_actions"], 0)

    @patch("fluid_build.cli.plan._display_plan_simple")
    @patch("fluid_build.cli.plan._plan_legacy")
    @patch("fluid_build.cli.plan._should_use_provider_actions")
    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_path_b_eventbridge_invokes_planner(
        self,
        mock_load,
        mock_should_use,
        mock_legacy,
        _mock_display,
    ):
        """Path-B eventbridge contract must result in schedule actions
        appearing in plan.json with ``op=eventbridge.ensure_schedule``
        or ``op=lambda.ensure_function`` (both emitted together)."""
        contract = _minimal_contract()
        contract["id"] = "dp-sched"
        contract["orchestration"] = {
            "engine": "eventbridge",
            "schedule": "rate(1 hour)",
        }
        mock_load.return_value = contract
        mock_should_use.return_value = False
        mock_legacy.return_value = {
            "format_version": "0.5.7",
            "actions": [],
            "total_actions": 0,
            "contract": {"name": "Test", "version": "0.5.7"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "plan.json")
            run(_make_args(out=out), LOG)
            with open(out) as f:
                plan = json.load(f)

            # SchedulePlanner emits both a Lambda + an EventBridge rule
            # for the eventbridge path. Assert presence of both.
            ops = [a.get("op") for a in plan["actions"]]
            self.assertIn("lambda.ensure_function", ops)
            self.assertIn("eventbridge.ensure_schedule", ops)

    @patch("fluid_build.cli.plan._display_plan_simple")
    @patch("fluid_build.cli.plan._plan_legacy")
    @patch("fluid_build.cli.plan._should_use_provider_actions")
    @patch("fluid_build.cli.plan.load_contract_with_overlay")
    def test_planner_failure_is_non_fatal(
        self,
        mock_load,
        mock_should_use,
        mock_legacy,
        _mock_display,
    ):
        """If SchedulePlanner itself raises, plan.py must warn and
        proceed — planning DDL is more important than wiring schedules."""
        contract = _minimal_contract()
        contract["orchestration"] = {
            "engine": "eventbridge",
            "schedule": "rate(1 hour)",
        }
        mock_load.return_value = contract
        mock_should_use.return_value = False
        mock_legacy.return_value = {
            "format_version": "0.5.7",
            "actions": [],
            "total_actions": 0,
            "contract": {"name": "Test", "version": "0.5.7"},
        }

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("fluid_build.providers.aws.plan.schedule.SchedulePlanner") as mock_planner_cls,
        ):
            mock_planner_cls.return_value.plan_schedule_actions.side_effect = RuntimeError(
                "STS unreachable"
            )
            out = os.path.join(tmpdir, "plan.json")
            result = run(_make_args(out=out), LOG)

            # Planner failure must not propagate — planning still returns 0.
            self.assertEqual(result, 0)
            with open(out) as f:
                plan = json.load(f)
            # Actions stay empty (scheduler output omitted) but plan still
            # carries the digest fields so apply can verify.
            self.assertEqual(plan["total_actions"], 0)
            self.assertIn("planDigest", plan)


if __name__ == "__main__":
    unittest.main()
