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

"""Tests for the ``--dbt-validate`` parse gate.

The gate wraps ``dbt parse`` in :mod:`fluid_build.cli.generate_speed_transformation`
and is consumed by the single-build and all-builds code paths. The tests
here cover the helper contract without spawning a real dbt process:

* ``_should_run_dbt_gate`` — gate predicate (flag AND engine == dbt).
* ``_run_dbt_parse_gate`` — dispatch to ``subprocess.run`` with the right
  project-dir argument; success/failure paths; missing-dbt fallback.

We monkeypatch ``shutil.which`` + ``subprocess.run`` to keep the tests
deterministic on CI runners without dbt installed.
"""

from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fluid_build.cli import generate_speed_transformation as gst


@pytest.fixture(autouse=True)
def _unset_dbt_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate now honours ``$DBT_EXECUTABLE`` (parity with the build
    runner) — keep these tests hermetic against a dev shell that sets it."""
    monkeypatch.delenv("DBT_EXECUTABLE", raising=False)


def test_should_run_dbt_gate_requires_flag_and_engine() -> None:
    # Flag off → skip regardless of engine.
    assert gst._should_run_dbt_gate(Namespace(dbt_validate=False), "dbt") is False
    # Flag on but engine is not dbt → skip (gate is dbt-only by design).
    assert gst._should_run_dbt_gate(Namespace(dbt_validate=True), "sql") is False
    # Flag on + engine dbt → run.
    assert gst._should_run_dbt_gate(Namespace(dbt_validate=True), "dbt") is True


def test_should_run_dbt_gate_tolerates_missing_attr() -> None:
    # argparse callers that forget the attribute entirely must not crash.
    assert gst._should_run_dbt_gate(SimpleNamespace(), "dbt") is False


def test_dbt_sql_model_paths_ignores_engine_owned_yaml() -> None:
    files = {
        "dbt_project.yml": "name: demo\n",
        "profiles.yml": "demo: {}\n",
        "models/sources.yml": "version: 2\n",
        "models/staging/stg_orders.sql": "select 1\n",
    }

    assert gst._dbt_sql_model_paths(files) == ["models/staging/stg_orders.sql"]


def test_generate_speed_transformation_fails_when_dbt_models_empty(tmp_path: Path) -> None:
    contract_path = tmp_path / "empty.fluid.yaml"
    contract_path.write_text(
        """
fluidVersion: 0.7.2
id: empty.dbt_v1
metadata:
  name: Empty dbt
  domain: test
  owner:
    team: data
builds:
  - id: main
    engine: dbt
    pattern: hybrid-reference
    repository: ./dbt_project
    properties:
      model: main
exposes: []
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "dbt_project"
    args = Namespace(
        list_engines=False,
        contract=str(contract_path),
        output=str(out_dir),
        build_index=0,
        model=None,
        all_builds=False,
        concurrency=4,
        overwrite=True,
        env=None,
        verbose=False,
        mesh_hub=None,
        dbt_validate=False,
        quiet=True,
    )

    assert gst.run(args, logging.getLogger("test")) == 1
    assert not list(out_dir.glob("models/**/*.sql"))


def test_run_dbt_parse_gate_returns_true_when_dbt_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # With dbt not installed we warn and return True so CI pipelines
    # without dbt can opt in without spurious failures. Discovery now
    # routes through the build runner's resolver (which also checks the
    # venv-sibling ``<python-dir>/dbt`` — present in this repo's dev
    # venv), so patch the resolver itself rather than ``shutil.which``.
    monkeypatch.setattr(
        "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
        lambda: None,
    )
    result = gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test"))
    assert result is True


def test_run_dbt_parse_gate_success_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", fake_run)

    result = gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test"))

    assert result is True
    fake_run.assert_called_once()
    args, _ = fake_run.call_args
    command = args[0]
    assert command[0] == "/usr/local/bin/dbt"
    assert command[1] == "parse"
    # Must pass --project-dir so dbt finds dbt_project.yml regardless of CWD.
    assert "--project-dir" in command
    assert str(tmp_path) in command


def test_run_dbt_parse_gate_injects_profiles_dir_when_profiles_yml_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the generator emitted a project-local ``profiles.yml``, the gate
    must point dbt at that directory via ``--profiles-dir`` so fresh users
    without a ``~/.dbt/profiles.yml`` don't see the gate fail at parse time.
    """
    # Simulate the generator having emitted a profiles.yml alongside the
    # dbt_project.yml in the output directory.
    (tmp_path / "profiles.yml").write_text(
        "retail_sales:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: target/dev.duckdb\n      threads: 4\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", fake_run)

    result = gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test"))

    assert result is True
    fake_run.assert_called_once()
    command = fake_run.call_args.args[0]
    # Both flags must be present; --profiles-dir must point at tmp_path.
    assert "--project-dir" in command
    assert "--profiles-dir" in command
    profiles_idx = command.index("--profiles-dir")
    assert command[profiles_idx + 1] == str(tmp_path)


def test_run_dbt_parse_gate_omits_profiles_dir_when_profiles_yml_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the project doesn't ship its own ``profiles.yml`` (e.g. the
    user manages ``~/.dbt/profiles.yml`` centrally), the gate must stay
    out of the way and let dbt's default resolution take over.
    """
    # tmp_path is empty by default — no profiles.yml present.
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", fake_run)

    result = gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test"))

    assert result is True
    fake_run.assert_called_once()
    command = fake_run.call_args.args[0]
    assert "--project-dir" in command
    # Must NOT inject --profiles-dir; dbt falls back to its default
    # (~/.dbt/profiles.yml or DBT_PROFILES_DIR env var).
    assert "--profiles-dir" not in command


def test_run_dbt_parse_gate_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(
        return_value=SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Compilation Error: model stg_orders references a non-existent source.",
        )
    )
    monkeypatch.setattr("subprocess.run", fake_run)

    result = gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test"))

    assert result is False


def test_run_dbt_parse_gate_returns_false_when_subprocess_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An OSError from subprocess.run (e.g. permission denied on the dbt
    # binary) should be caught and surfaced as a gate failure, not a
    # crash.
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")

    def _boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("subprocess.run", _boom)

    result = gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test"))

    assert result is False


# ---------------------------------------------------------------------------
# packages.yml → `dbt deps` before `dbt parse` (regression)
#
# #425 emits packages.yml for any project using a namespaced test
# (dbt_utils.recency, dbt_expectations.*) — i.e. most non-trivial contracts.
# dbt then refuses to parse at all until the packages are installed:
#
#   Compilation Error / dbt expects 1 package(s) based on packages specified
#   in packages.yml, but found only 0 package(s) installed in dbt_packages.
#   Run "dbt deps" to install package dependencies.
#
# so `--dbt-validate` failed on a chore instead of on the user's contract.
# ---------------------------------------------------------------------------


def _packages_project(tmp_path: Path) -> Path:
    (tmp_path / "packages.yml").write_text(
        "packages:\n- package: dbt-labs/dbt_utils\n  version: 1.4.0\n", encoding="utf-8"
    )
    return tmp_path


def test_gate_runs_dbt_deps_before_parse_when_packages_yml_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _packages_project(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", fake_run)

    assert gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test")) is True

    commands = [call.args[0] for call in fake_run.call_args_list]
    assert [c[1] for c in commands] == ["deps", "parse"]
    for command in commands:
        assert "--project-dir" in command


def test_gate_skips_dbt_deps_when_packages_are_already_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _packages_project(tmp_path)
    (tmp_path / "dbt_packages").mkdir()
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", fake_run)

    assert gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test")) is True
    assert [call.args[0][1] for call in fake_run.call_args_list] == ["parse"]


def test_gate_reports_a_dbt_deps_failure_distinctly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _packages_project(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(
        return_value=SimpleNamespace(returncode=1, stdout="hub unreachable", stderr="")
    )
    monkeypatch.setattr("subprocess.run", fake_run)

    assert gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test")) is False
    # Only `dbt deps` ran — parse is not attempted against an unresolved project.
    assert [call.args[0][1] for call in fake_run.call_args_list] == ["deps"]
    assert "dbt deps failed" in capsys.readouterr().out


def test_gate_without_packages_yml_still_runs_parse_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/dbt")
    fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", fake_run)

    assert gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test")) is True
    assert [call.args[0][1] for call in fake_run.call_args_list] == ["parse"]
