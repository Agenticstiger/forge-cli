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

"""Pins for ``fluid_build.describe`` + ``fluid describe --self`` + ``latest_schema_path``."""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from fluid_build import describe as describe_mod
from fluid_build.cli import describe_cmd
from fluid_build.describe import self_describe
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

_EXPECTED_KEYS = {
    "fluid_version",
    "python_version",
    "schema_version",
    "providers",
    "build_engines",
    "templates",
    "provider_engine_compatibility",
    "capabilities",
    "warnings",
}


def test_latest_schema_path_exists_on_disk() -> None:
    path = FluidSchemaManager.latest_schema_path()
    assert path.exists(), f"latest_schema_path() points at a missing file: {path}"
    assert path.name == f"fluid-schema-{FluidSchemaManager.latest_bundled_version()}.json"


def test_self_describe_has_all_keys_with_correct_types() -> None:
    info = self_describe()
    assert set(info) == _EXPECTED_KEYS
    assert isinstance(info["fluid_version"], str)
    assert isinstance(info["python_version"], str)
    assert isinstance(info["schema_version"], str)
    assert isinstance(info["providers"], list)
    assert isinstance(info["build_engines"], list)
    assert isinstance(info["templates"], list)
    assert isinstance(info["provider_engine_compatibility"], dict)
    assert isinstance(info["capabilities"], dict)
    assert isinstance(info["warnings"], list)


def test_self_describe_is_json_serializable() -> None:
    json.dumps(self_describe())  # must not raise


def test_capabilities_are_derived_not_hardcoded() -> None:
    caps = self_describe()["capabilities"]
    # Every advertised capability maps to a real importable module, so each
    # flag must be True in a normally-installed checkout.
    assert set(caps) == set(describe_mod._CAPABILITY_MODULES)
    assert all(isinstance(v, bool) for v in caps.values())
    assert caps["engine_api"] is True


def test_unknown_capability_module_reports_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(describe_mod._CAPABILITY_MODULES, "phantom", "fluid_build._does_not_exist")
    assert self_describe()["capabilities"]["phantom"] is False


def test_self_describe_degrades_when_matrix_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> dict:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr("fluid_build.cli.forge_copilot_runtime.build_capability_matrix", _boom)
    info = self_describe()
    assert info["providers"] == []
    assert "capability matrix unavailable" in info["warnings"]
    # capability detection is independent of the matrix and still runs
    assert info["capabilities"]["engine_api"] is True


def test_cli_self_emits_json_with_flag(capsys: pytest.CaptureFixture[str]) -> None:
    rc = describe_cmd.run(Namespace(describe_self=True, as_json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == _EXPECTED_KEYS


def test_cli_self_human_readable_default(capsys: pytest.CaptureFixture[str]) -> None:
    rc = describe_cmd.run(Namespace(describe_self=True, as_json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "FLUID forge-cli" in out
    assert "Capabilities:" in out


def test_cli_without_self_flag_returns_usage(capsys: pytest.CaptureFixture[str]) -> None:
    rc = describe_cmd.run(Namespace(describe_self=False, as_json=False))
    assert rc == 1
    assert "Usage:" in capsys.readouterr().out
