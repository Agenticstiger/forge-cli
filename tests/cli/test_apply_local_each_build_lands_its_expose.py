# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply`` on the local engine writes each build's rows into the expose it names.

Two ``engine: sql`` builds, each naming its own expose in ``outputs``. Applying
the recorded ``plan.json`` from another directory used to answer the plan's
abstract ops with ONE derived SQL action (the first build's SQL into the first
expose's file), exit 0, and report "Actions Applied 5" for a 4-action plan. The
YAML route handed the second build the first build's result table as an input
file ("Input file not found: result_build_a").
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.cli.apply import run as apply_run
from fluid_build.cli.plan import run as plan_run

pytestmark = [pytest.mark.integration]

LOG = logging.getLogger("test.apply_local_each_build")


def _build(build_id: str, sql: str, output: str) -> Dict[str, Any]:
    return {
        "id": build_id,
        "pattern": "embedded-logic",
        "engine": "sql",
        "properties": {"sql": sql},
        "outputs": [output],
    }


def _expose(expose_id: str, column: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": f"out/{expose_id}.csv"},
        },
        "contract": {"schema": [{"name": column, "type": "integer"}]},
    }


def _contract(a_id: str = "build_a", b_id: str = "build_b") -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "repro.two_builds_v1",
        "name": "Two builds",
        "domain": "repro",
        "metadata": {"layer": "Bronze", "owner": {"team": "repro-team"}},
        "builds": [
            _build(a_id, "SELECT 1 AS a", "A"),
            _build(b_id, "SELECT 2 AS b", "B"),
        ],
        "exposes": [_expose("A", "a"), _expose("B", "b")],
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("FLUID_ENV", "FLUID_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


def _write(project: Path, contract: Dict[str, Any]) -> None:
    (project / "contract.fluid.yaml").write_text(yaml.safe_dump(contract, sort_keys=False), "utf-8")


def _parse(*argv: str) -> Any:
    return build_parser().parse_args(list(argv))


def _landed(project: Path) -> Dict[str, str]:
    return {p.name: p.read_text("utf-8") for p in sorted((project / "out").glob("*.csv"))}


def test_the_contract_file_lands_each_build_in_its_own_expose(project: Path) -> None:
    _write(project, _contract())
    assert apply_run(_parse("apply", "contract.fluid.yaml", "--yes"), LOG) == 0
    assert _landed(project) == {"A.csv": "a\n1\n", "B.csv": "b\n2\n"}


def test_a_plan_applied_from_another_directory_lands_each_build_in_its_own_expose(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _write(project, _contract())
    assert plan_run(_parse("plan", "contract.fluid.yaml", "--out", "plan.json"), LOG) == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert apply_run(_parse("apply", str(project / "plan.json"), "--yes"), LOG) == 0

    assert _landed(project) == {"A.csv": "a\n1\n", "B.csv": "b\n2\n"}


def test_a_plan_applied_with_a_hyphenated_build_id_lands_real_rows(project: Path) -> None:
    # The table ``result_hello-transformation`` failed validate_ident, the
    # failure was swallowed, and the expose was written as a placeholder.
    _write(project, _contract(a_id="hello-transformation"))
    assert apply_run(_parse("apply", "contract.fluid.yaml", "--yes"), LOG) == 0
    assert _landed(project)["A.csv"] == "a\n1\n"


def test_skipped_actions_are_not_counted_as_applied(tmp_path: Path) -> None:
    from fluid_build.providers.local.local import LocalProvider

    out = tmp_path / "o.csv"
    provider = LocalProvider(project="t", region="local", anchor_dir=tmp_path)
    result = provider.apply(
        actions=[
            {"op": "noop", "skipped": True},
            {"op": "noop", "skipped": True},
            {"op": "sql", "sql": "SELECT 1 AS x", "outputs": [{"path": str(out), "format": "csv"}]},
        ]
    )
    assert (result["applied"], result["failed"]) == (1, 0)
    assert sum(1 for r in result["results"] if r.get("skipped")) == 2
