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

"""``fluid apply`` schema-validates the contract on EVERY engine and mode.

The pre-apply gate (``_gate_contract_for_apply``: pre-0.7 rejection plus
JSON-schema validation) used to sit only on the native engine's non-build
path. Two early returns ran above it:

* ``apply_engine == "opentofu"`` (aws, gcp, snowflake, confluent and every
  IaC plugin cloud), so a schema-invalid contract reached ``tofu`` and could
  apply with exit 0;
* ``needs_build(mode)`` on the native engine, so ``--mode amend-and-build`` /
  ``replace-and-build`` ran the contract's builds without validating it.

These tests drive ``run()`` through the real argparse surface; only the
OpenTofu engine and the build runner are replaced by recorders, so "the gate
ran first" is asserted as "the engine / runner was never called".
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest import mock

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import register as register_apply
from fluid_build.cli.apply import run as apply_run
from fluid_build.forge.core.plan_digest import inject_digests

LOG = logging.getLogger("test.apply_schema_gate_every_engine")

_VALID: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "demo.schema_gate",
    "name": "Schema Gate Demo",
    "description": "Inline SQL build.",
    "domain": "Demo",
    "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@example.com"}},
    "builds": [
        {
            "id": "make_rows",
            "description": "Inline rows.",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1 AS id"},
            "outputs": ["rows"],
        }
    ],
    "exposes": [
        {
            "exposeId": "rows",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "./out/rows.parquet"},
            },
            "contract": {"schema": [{"name": "id", "type": "INTEGER", "required": True}]},
        }
    ],
}


def _invalid() -> Dict[str, Any]:
    """The card's example: an out-of-enum ``orchestration.engine``."""
    contract = copy.deepcopy(_VALID)
    contract["orchestration"] = {"engine": "not-a-real-engine"}
    return contract


def _pre_07() -> Dict[str, Any]:
    contract = copy.deepcopy(_VALID)
    contract["fluidVersion"] = "0.5.7"
    return contract


def _parse(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fluid")
    parser.add_argument("--env", default=None)
    sub = parser.add_subparsers(dest="cmd")
    register_apply(sub)
    return parser.parse_args(argv)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    (tmp_path / "runtime").mkdir()
    return tmp_path


def _write_contract(workspace: Path, contract: Dict[str, Any]) -> str:
    path = workspace / "contract.fluid.yaml"
    path.write_text(json.dumps(contract), encoding="utf-8")
    return "contract.fluid.yaml"


def _write_plan(workspace: Path, contract: Dict[str, Any], *, mode: str) -> str:
    plan = {
        "format_version": "0.7.5",
        "generated_at": 1700000000,
        "mode": mode,
        "contract": copy.deepcopy(contract),
        "contract_metadata": {"id": contract["id"]},
        "actions": [],
        "total_actions": 0,
    }
    plan = inject_digests(plan, bundle_path=None)
    (workspace / "runtime" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return "runtime/plan.json"


@pytest.fixture
def tofu() -> Iterator[mock.Mock]:
    """Route ``run()`` onto the OpenTofu engine and record the engine call."""
    with (
        mock.patch(
            "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine",
            return_value="opentofu",
        ),
        mock.patch(
            "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu", return_value=0
        ) as engine,
    ):
        yield engine


@pytest.fixture
def builds() -> Iterator[mock.Mock]:
    with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as runner:
        yield runner


@pytest.fixture
def hooks() -> Iterator[mock.Mock]:
    with mock.patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as hook:
        yield hook


def _native() -> Any:
    return mock.patch(
        "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine", return_value="native"
    )


class TestOpenTofuEngineIsGated:
    @pytest.mark.parametrize("mode", ["amend", "dry-run", "amend-and-build"])
    def test_schema_invalid_yaml_never_reaches_tofu(
        self,
        workspace: Path,
        tofu: mock.Mock,
        builds: mock.Mock,
        hooks: mock.Mock,
        mode: str,
    ) -> None:
        src = _write_contract(workspace, _invalid())
        with pytest.raises(CLIError) as exc:
            apply_run(_parse(["apply", src, "--mode", mode, "--yes"]), LOG)
        assert exc.value.event == "apply_contract_invalid"
        assert any("orchestration" in str(e) for e in exc.value.context["errors"])
        tofu.assert_not_called()
        builds.assert_not_called()
        hooks.assert_not_called()

    def test_schema_invalid_contract_embedded_in_a_plan_never_reaches_tofu(
        self, workspace: Path, tofu: mock.Mock, hooks: mock.Mock
    ) -> None:
        plan = _write_plan(workspace, _invalid(), mode="amend")
        with pytest.raises(CLIError) as exc:
            apply_run(_parse(["apply", plan, "--mode", "amend", "--yes"]), LOG)
        assert exc.value.event == "apply_contract_invalid"
        tofu.assert_not_called()
        hooks.assert_not_called()

    def test_pre_07_contract_never_reaches_tofu(
        self, workspace: Path, tofu: mock.Mock, hooks: mock.Mock
    ) -> None:
        src = _write_contract(workspace, _pre_07())
        with pytest.raises(CLIError) as exc:
            apply_run(_parse(["apply", src, "--yes"]), LOG)
        assert exc.value.event == "contract_version_unsupported"
        tofu.assert_not_called()

    def test_valid_contract_still_reaches_tofu_once(
        self, workspace: Path, tofu: mock.Mock, hooks: mock.Mock
    ) -> None:
        src = _write_contract(workspace, _VALID)
        rc = apply_run(_parse(["apply", src, "--yes"]), LOG)
        assert rc == 0
        tofu.assert_called_once()
        hooks.assert_called_once()

    def test_valid_plan_still_reaches_tofu_once(self, workspace: Path, tofu: mock.Mock) -> None:
        plan = _write_plan(workspace, _VALID, mode="amend")
        rc = apply_run(_parse(["apply", plan, "--mode", "amend", "--yes"]), LOG)
        assert rc == 0
        tofu.assert_called_once()


class TestNativeBuildModesAreGated:
    @pytest.mark.parametrize(
        "mode,extra",
        [("amend-and-build", []), ("replace-and-build", ["--allow-data-loss"])],
    )
    def test_schema_invalid_yaml_never_reaches_the_build_runner(
        self, workspace: Path, builds: mock.Mock, mode: str, extra: List[str]
    ) -> None:
        src = _write_contract(workspace, _invalid())
        with _native(), pytest.raises(CLIError) as exc:
            apply_run(_parse(["apply", src, "--mode", mode, "--yes", *extra]), LOG)
        assert exc.value.event == "apply_contract_invalid"
        builds.assert_not_called()

    def test_schema_invalid_plan_never_reaches_the_build_runner(
        self, workspace: Path, builds: mock.Mock
    ) -> None:
        plan = _write_plan(workspace, _invalid(), mode="amend-and-build")
        with _native(), pytest.raises(CLIError) as exc:
            apply_run(_parse(["apply", plan, "--mode", "amend-and-build", "--yes"]), LOG)
        assert exc.value.event == "apply_contract_invalid"
        builds.assert_not_called()

    def test_pre_07_contract_never_reaches_the_build_runner(
        self, workspace: Path, builds: mock.Mock
    ) -> None:
        src = _write_contract(workspace, _pre_07())
        with _native(), pytest.raises(CLIError) as exc:
            apply_run(_parse(["apply", src, "--mode", "amend-and-build", "--yes"]), LOG)
        assert exc.value.event == "contract_version_unsupported"
        builds.assert_not_called()

    def test_valid_contract_still_reaches_the_build_runner(
        self, workspace: Path, builds: mock.Mock
    ) -> None:
        src = _write_contract(workspace, _VALID)
        with _native():
            rc = apply_run(_parse(["apply", src, "--mode", "amend-and-build", "--yes"]), LOG)
        assert rc == 0
        builds.assert_called_once()


class TestExactlyOneGatePerRun:
    """The gate is hoisted, not duplicated: one call whatever the route."""

    def _count(self) -> Any:
        from fluid_build.cli import apply as apply_cli

        return mock.patch(
            "fluid_build.cli.apply._gate_contract_for_apply",
            wraps=apply_cli._gate_contract_for_apply,
        )

    def test_opentofu_engine(self, workspace: Path, tofu: mock.Mock, hooks: mock.Mock) -> None:
        src = _write_contract(workspace, _VALID)
        with self._count() as gate:
            apply_run(_parse(["apply", src, "--yes"]), LOG)
        assert gate.call_count == 1

    def test_native_build_mode(self, workspace: Path, builds: mock.Mock) -> None:
        src = _write_contract(workspace, _VALID)
        with _native(), self._count() as gate:
            apply_run(_parse(["apply", src, "--mode", "amend-and-build", "--yes"]), LOG)
        assert gate.call_count == 1

    def test_native_yaml_apply_loads_and_gates_once(self, workspace: Path) -> None:
        """The native path applies the very contract the gate validated —
        one load, one gate, no re-read between them."""
        from fluid_build.cli import apply as apply_cli

        src = _write_contract(workspace, _VALID)
        with (
            _native(),
            self._count() as gate,
            mock.patch(
                "fluid_build.cli.apply.load_contract_with_overlay",
                wraps=apply_cli.load_contract_with_overlay,
            ) as load,
            mock.patch("fluid_build.cli.apply._run_simple_apply", return_value=0) as simple,
        ):
            rc = apply_run(_parse(["apply", src, "--yes"]), LOG)
        assert rc == 0
        assert gate.call_count == 1
        assert load.call_count == 1
        assert simple.call_args.kwargs["contract"]["id"] == _VALID["id"]

    def test_native_plan_apply(self, workspace: Path) -> None:
        plan = _write_plan(workspace, _VALID, mode="amend")
        with (
            _native(),
            self._count() as gate,
            mock.patch("fluid_build.cli.apply._run_simple_apply", return_value=0),
        ):
            apply_run(_parse(["apply", plan, "--mode", "amend", "--yes"]), LOG)
        assert gate.call_count == 1


class TestUnloadableSource:
    def test_unparseable_yaml_is_a_typed_error_before_tofu(
        self, workspace: Path, tofu: mock.Mock
    ) -> None:
        (workspace / "broken.fluid.yaml").write_text("id: [unclosed\n", encoding="utf-8")
        with pytest.raises(CLIError) as exc:
            apply_run(_parse(["apply", "broken.fluid.yaml", "--yes"]), LOG)
        assert exc.value.event == "apply_execution_failed"
        tofu.assert_not_called()

    def test_plan_without_an_embedded_contract_defers_to_the_engine(
        self, workspace: Path, tofu: mock.Mock
    ) -> None:
        """No contract to validate: the engine keeps owning that error
        (``opentofu_engine_no_contract``), so the gate must not invent one."""
        plan = inject_digests(
            {"format_version": "0.7.5", "mode": "amend", "actions": [], "total_actions": 0},
            bundle_path=None,
        )
        (workspace / "runtime" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        apply_run(_parse(["apply", "runtime/plan.json", "--mode", "amend", "--yes"]), LOG)
        tofu.assert_called_once()
