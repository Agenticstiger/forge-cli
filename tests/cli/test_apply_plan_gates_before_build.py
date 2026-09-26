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

"""Stage-7 plan gates run before ANY build, on both engine paths.

``fluid apply plan.json --mode amend-and-build`` used to return into the
build runner before the planDigest check and the plan/apply mode check ran,
so a tampered plan's builds executed with exit 0 while ``--mode amend``
refused the same file. ``--build-id`` with a mode that runs no build was
accepted and silently dropped.

Every test drives ``fluid apply``'s real ``run()`` with the real argparse
surface; only the build runner and the OpenTofu engine are replaced by
recorders, so "the gate ran first" is asserted as "the build was never
called".
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import register as register_apply
from fluid_build.cli.apply import run as apply_run
from fluid_build.forge.core.plan_digest import inject_digests

LOG = logging.getLogger("test.apply_plan_gates")

_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "demo.gates",
    "name": "Gates Demo",
    "description": "Inline SQL build.",
    "domain": "Demo",
    "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@example.com"}},
    "builds": [
        {
            "id": "make_rows",
            "description": "Inline rows.",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1 AS id, 'a' AS name"},
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
            "contract": {
                "schema": [
                    {"name": "id", "type": "INTEGER", "required": True},
                    {"name": "name", "type": "VARCHAR", "required": False},
                ]
            },
        }
    ],
}


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
    contract_dir = tmp_path / "contracts" / "p"
    contract_dir.mkdir(parents=True)
    (contract_dir / "contract.fluid.yaml").write_text(json.dumps(_CONTRACT), encoding="utf-8")
    (tmp_path / "runtime").mkdir()
    return tmp_path


def _write_plan(workspace: Path, *, mode: Any, tamper: bool = False) -> str:
    plan = {
        "format_version": "0.7.5",
        "generated_at": 1700000000,
        "mode": mode,
        "contract": json.loads(json.dumps(_CONTRACT)),
        "contract_metadata": {
            "id": _CONTRACT["id"],
            "source_path": str(workspace / "contracts" / "p" / "contract.fluid.yaml"),
        },
        "actions": [],
        "total_actions": 0,
    }
    plan = inject_digests(plan, bundle_path=None)
    if tamper:
        plan["contract"]["builds"][0]["properties"]["sql"] = "SELECT 666 AS id, 'x' AS name"
    path = workspace / "runtime" / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return "runtime/plan.json"


class TestPlanGatesRunBeforeBuilds:
    @pytest.mark.parametrize(
        "mode,extra",
        [("amend-and-build", []), ("replace-and-build", ["--allow-data-loss"])],
    )
    def test_tampered_plan_never_reaches_the_build_runner(
        self, workspace: Path, mode: str, extra: List[str]
    ) -> None:
        plan = _write_plan(workspace, mode=mode, tamper=True)
        args = _parse(["apply", plan, "--mode", mode, "--yes", *extra])
        with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as rb:
            with pytest.raises(CLIError) as exc:
                apply_run(args, LOG)
        assert exc.value.event == "apply_plan_digest_plan_tamper"
        rb.assert_not_called()

    def test_mode_mismatch_never_reaches_the_build_runner(self, workspace: Path) -> None:
        plan = _write_plan(workspace, mode=None)
        args = _parse(["apply", plan, "--mode", "amend-and-build", "--yes"])
        with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as rb:
            with pytest.raises(CLIError) as exc:
                apply_run(args, LOG)
        assert exc.value.event == "apply_plan_mode_mismatch"
        assert exc.value.context["requested_mode"] == "amend-and-build"
        rb.assert_not_called()

    def test_opentofu_path_gates_before_tofu_and_build(self, workspace: Path) -> None:
        """The cloud engine path returned into the build runner without the mode check."""
        plan = _write_plan(workspace, mode=None)
        args = _parse(["apply", plan, "--mode", "amend-and-build", "--yes"])
        with (
            mock.patch(
                "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine",
                return_value="opentofu",
            ),
            mock.patch(
                "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu", return_value=0
            ) as tofu,
            mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as rb,
        ):
            with pytest.raises(CLIError) as exc:
                apply_run(args, LOG)
        assert exc.value.event == "apply_plan_mode_mismatch"
        tofu.assert_not_called()
        rb.assert_not_called()

    def test_verified_plan_is_handed_to_the_build_runner(self, workspace: Path) -> None:
        plan = _write_plan(workspace, mode="amend-and-build")
        attested = json.loads((workspace / plan).read_text(encoding="utf-8"))
        args = _parse(["apply", plan, "--mode", "amend-and-build", "--yes"])
        with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as rb:
            assert apply_run(args, LOG) == 0
        rb.assert_called_once()
        assert rb.call_args.kwargs.get("plan_data") == attested


class TestUnreadablePlan:
    """A plan.json that is not JSON is a typed exit 1, on every mode.

    Loading the plan moved ahead of the build return, outside any handler,
    so a truncated or non-UTF-8 file became "CLI unhandled exception" (exit
    2) instead of the typed error both paths used to give.
    """

    @pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe{}"], ids=["not-json", "not-utf8"])
    @pytest.mark.parametrize("mode", ["amend", "amend-and-build"])
    def test_malformed_plan_is_a_typed_error(
        self, workspace: Path, content: bytes, mode: str
    ) -> None:
        plan = workspace / "runtime" / "broken.json"
        plan.write_bytes(content)
        args = _parse(["apply", "runtime/broken.json", "--mode", mode, "--yes"])
        with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as rb:
            with pytest.raises(CLIError) as exc:
                apply_run(args, LOG)
        assert exc.value.event == "apply_plan_unreadable"
        assert exc.value.exit_code == 1
        assert Path(exc.value.context["path"]).name == "broken.json"
        rb.assert_not_called()


class TestBuildIdNeedsABuildMode:
    @pytest.mark.parametrize(
        "mode,extra",
        [
            ("amend", []),
            ("dry-run", []),
            ("create-only", []),
            ("replace", ["--allow-data-loss"]),
        ],
    )
    def test_build_id_with_a_non_build_mode_is_refused(
        self, workspace: Path, mode: str, extra: List[str]
    ) -> None:
        args = _parse(
            [
                "apply",
                "contracts/p/contract.fluid.yaml",
                "--mode",
                mode,
                "--build-id",
                "make_rows",
                "--yes",
                *extra,
            ]
        )
        with (
            mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as rb,
            mock.patch("fluid_build.cli.apply._run_simple_apply", return_value=0) as simple,
        ):
            with pytest.raises(CLIError) as exc:
                apply_run(args, LOG)
        assert exc.value.event == "apply_build_id_requires_build_mode"
        assert exc.value.context["mode"] == mode
        rb.assert_not_called()
        simple.assert_not_called()

    def test_build_id_with_a_build_mode_filters_the_build(self, workspace: Path) -> None:
        args = _parse(
            [
                "apply",
                "contracts/p/contract.fluid.yaml",
                "--mode",
                "amend-and-build",
                "--build-id",
                "make_rows",
                "--yes",
            ]
        )
        with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as rb:
            assert apply_run(args, LOG) == 0
        rb.assert_called_once()
        assert rb.call_args.args[0].build_id == "make_rows"
