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

"""Tests for fluid_build.cli.execute."""

import argparse
import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.execute import execute_build, resolve_script_path, run

# ── resolve_script_path ───────────────────────────────────────────────


class TestResolveScriptPath:
    def test_returns_py_file_when_exists(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        script = repo_dir / "ingest.py"
        script.touch()

        build = {"repository": "repo", "properties": {"model": "ingest"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == script

    def test_returns_file_without_extension(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        script = repo_dir / "ingest"
        script.touch()

        build = {"repository": "repo", "properties": {"model": "ingest"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == script

    def test_returns_none_when_script_missing(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        build = {"repository": "repo", "properties": {"model": "missing_model"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result is None

    def test_uses_default_repository_and_model(self, tmp_path):
        # defaults: repository="./" model="ingest"
        ingest_py = tmp_path / "ingest.py"
        ingest_py.touch()

        build = {}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == ingest_py

    def test_prefers_py_extension_over_bare_file(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        script_py = repo_dir / "transform.py"
        script_py.touch()
        script_bare = repo_dir / "transform"
        script_bare.touch()

        build = {"repository": "repo", "properties": {"model": "transform"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == script_py


# ── execute_build ─────────────────────────────────────────────────────


class TestExecuteBuild:
    def _make_script(self, tmp_path, name="ingest.py"):
        s = tmp_path / name
        s.touch()
        return s

    def test_dry_run_manual_returns_zero(self, tmp_path):
        build = {
            "id": "test-build",
            "execution": {"trigger": {"type": "manual", "iterations": 3}},
        }
        script = self._make_script(tmp_path)
        with patch("fluid_build.cli.execute.cprint"):
            result = execute_build(build, script, tmp_path, dry_run=True)
        assert result == 0

    def test_schedule_trigger_returns_zero(self, tmp_path):
        build = {
            "id": "sched-build",
            "execution": {"trigger": {"type": "schedule", "cron": "0 0 * * *"}},
        }
        script = self._make_script(tmp_path)
        with patch("fluid_build.cli.execute.cprint"):
            result = execute_build(build, script, tmp_path)
        assert result == 0

    def test_unknown_trigger_returns_one(self, tmp_path):
        build = {
            "id": "bad-build",
            "execution": {"trigger": {"type": "unknown_type"}},
        }
        script = self._make_script(tmp_path)
        with patch("fluid_build.cli.execute.cprint"):
            result = execute_build(build, script, tmp_path)
        assert result == 1

    def test_successful_run_returns_zero(self, tmp_path):
        build = {
            "id": "ok-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")

        with (
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.subprocess.run", return_value=mock_result),
        ):
            result = execute_build(build, script, tmp_path, delay=0)
        assert result == 0

    def test_failed_run_returns_one(self, tmp_path):
        build = {
            "id": "fail-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=1, stderr="error!")

        with (
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.console_error"),
            patch("fluid_build.cli.execute.subprocess.run", return_value=mock_result),
        ):
            result = execute_build(build, script, tmp_path, delay=0)
        assert result == 1

    def test_fail_fast_stops_on_first_failure(self, tmp_path):
        build = {
            "id": "fail-fast-build",
            "execution": {"trigger": {"type": "manual", "iterations": 3}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=1, stderr="")

        with (
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.console_error"),
            patch("fluid_build.cli.execute.subprocess.run", return_value=mock_result),
        ):
            result = execute_build(build, script, tmp_path, delay=0, fail_fast=True)
        assert result == 1

    def test_exception_during_run_with_fail_fast(self, tmp_path):
        build = {
            "id": "exc-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)

        with (
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.console_error"),
            patch("fluid_build.cli.execute.subprocess.run", side_effect=RuntimeError("boom")),
        ):
            result = execute_build(build, script, tmp_path, delay=0, fail_fast=True)
        assert result == 1

    def test_delay_between_iterations(self, tmp_path):
        build = {
            "id": "multi-build",
            "execution": {"trigger": {"type": "manual", "iterations": 2}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")

        with (
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.subprocess.run", return_value=mock_result),
            patch("fluid_build.cli.execute.time.sleep") as mock_sleep,
        ):
            execute_build(build, script, tmp_path, delay=5)
        mock_sleep.assert_called_once_with(5)

    def test_contract_delay_overrides_arg(self, tmp_path):
        """delaySeconds in trigger overrides the delay arg."""
        build = {
            "id": "delay-contract-build",
            "execution": {"trigger": {"type": "manual", "iterations": 2, "delaySeconds": 10}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")

        with (
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.subprocess.run", return_value=mock_result),
            patch("fluid_build.cli.execute.time.sleep") as mock_sleep,
        ):
            execute_build(build, script, tmp_path, delay=2)
        mock_sleep.assert_called_once_with(10)

    def test_venv_python_used_when_available(self, tmp_path):
        build = {
            "id": "venv-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")
        venv_python = tmp_path / "bin" / "python3"
        venv_python.parent.mkdir()
        venv_python.touch()

        captured_calls = []

        def fake_run(cmd, **kwargs):
            captured_calls.append(cmd)
            return mock_result

        with (
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.subprocess.run", side_effect=fake_run),
            patch.dict("os.environ", {"VIRTUAL_ENV": str(tmp_path)}),
        ):
            execute_build(build, script, tmp_path, delay=0)

        assert captured_calls[0][0] == str(venv_python)

    def test_no_output_stderr_shown_on_failure(self, tmp_path):
        build = {
            "id": "noout-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=1, stderr="some error text")
        printed = []

        with (
            patch("fluid_build.cli.execute.cprint", side_effect=lambda m: printed.append(m)),
            patch("fluid_build.cli.execute.console_error"),
            patch("fluid_build.cli.execute.subprocess.run", return_value=mock_result),
        ):
            execute_build(build, script, tmp_path, delay=0, no_output=True)

        assert any("some error text" in str(p) for p in printed)


# ── run ───────────────────────────────────────────────────────────────


class TestRun:
    def _args(self, **kwargs):
        defaults = {
            "contract": "/nonexistent/contract.yaml",
            "build_id": None,
            "dry_run": False,
            "delay": 2,
            "no_output": False,
            "fail_fast": False,
            "env": None,
        }
        defaults.update(kwargs)
        ns = argparse.Namespace(**defaults)
        return ns

    def test_missing_contract_raises_cli_error(self, tmp_path):
        args = self._args(contract=str(tmp_path / "missing.yaml"))
        logger = logging.getLogger("test")
        with pytest.raises(CLIError) as exc_info:
            run(args, logger)
        assert exc_info.value.event == "contract_not_found"

    def test_empty_builds_returns_zero(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.cli.execute.load_contract_with_overlay",
                return_value={"builds": []},
            ),
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.console_error"),
        ):
            result = run(args, logger)
        assert result == 0

    def test_build_id_filter_not_found_returns_one(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        args = self._args(contract=str(contract_file), build_id="nonexistent")
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.cli.execute.load_contract_with_overlay",
                return_value={"builds": [{"id": "other-build"}]},
            ),
            patch("fluid_build.cli.execute.cprint"),
        ):
            result = run(args, logger)
        assert result == 1

    def test_contract_load_exception_raises_cli_error(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with patch(
            "fluid_build.cli.execute.load_contract_with_overlay",
            side_effect=ValueError("bad yaml"),
        ):
            with pytest.raises(CLIError) as exc_info:
                run(args, logger)
        assert exc_info.value.event == "contract_load_failed"

    def test_script_not_found_skipped(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {"id": "b1", "repository": "repo", "properties": {"model": "missing"}}
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.cli.execute.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.console_error"),
        ):
            result = run(args, logger)
        assert result == 0

    def test_all_builds_succeed_returns_zero(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {"id": "b1", "execution": {"trigger": {"type": "manual"}}}
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        with (
            patch(
                "fluid_build.cli.execute.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch("fluid_build.cli.execute.resolve_script_path", return_value=script_path),
            patch("fluid_build.cli.execute.execute_build", return_value=0),
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.console_error"),
        ):
            result = run(args, logger)
        assert result == 0

    def test_failed_build_returns_one(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {"id": "b1", "execution": {"trigger": {"type": "manual"}}}
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        with (
            patch(
                "fluid_build.cli.execute.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch("fluid_build.cli.execute.resolve_script_path", return_value=script_path),
            patch("fluid_build.cli.execute.execute_build", return_value=1),
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.console_error"),
        ):
            result = run(args, logger)
        assert result == 1

    def test_fail_fast_stops_after_first_failed_build(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        builds = [
            {"id": "b1", "execution": {"trigger": {"type": "manual"}}},
            {"id": "b2", "execution": {"trigger": {"type": "manual"}}},
        ]
        args = self._args(contract=str(contract_file), fail_fast=True)
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        execute_build_calls = []

        def fake_execute_build(*a, **kw):
            execute_build_calls.append(a[0].get("id"))
            return 1

        with (
            patch(
                "fluid_build.cli.execute.load_contract_with_overlay",
                return_value={"builds": builds},
            ),
            patch("fluid_build.cli.execute.resolve_script_path", return_value=script_path),
            patch("fluid_build.cli.execute.execute_build", side_effect=fake_execute_build),
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.console_error"),
        ):
            result = run(args, logger)

        assert result == 1
        assert len(execute_build_calls) == 1

    def test_build_id_filter_matches(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        builds = [
            {"id": "b1", "execution": {"trigger": {"type": "manual"}}},
            {"id": "b2", "execution": {"trigger": {"type": "manual"}}},
        ]
        args = self._args(contract=str(contract_file), build_id="b2")
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        executed_ids = []

        def fake_execute_build(build, *a, **kw):
            executed_ids.append(build.get("id"))
            return 0

        with (
            patch(
                "fluid_build.cli.execute.load_contract_with_overlay",
                return_value={"builds": builds},
            ),
            patch("fluid_build.cli.execute.resolve_script_path", return_value=script_path),
            patch("fluid_build.cli.execute.execute_build", side_effect=fake_execute_build),
            patch("fluid_build.cli.execute.cprint"),
            patch("fluid_build.cli.execute.success"),
            patch("fluid_build.cli.execute.console_error"),
        ):
            result = run(args, logger)

        assert result == 0
        assert executed_ids == ["b2"]
