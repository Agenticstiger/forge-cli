# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The local engine never writes a placeholder file for rows it does not have.

``fluid init qs --quickstart --yes && fluid apply contract.fluid.yaml --yes``
logged "Plan validation failed with 2 errors", then "Data product deployed
successfully" with exit 0, and left 24-byte parquet files holding ``id,value /
1,materialized``. The same placeholder stood in for an expose whose table could
not be created (a build id with a hyphen failed ``validate_ident``, and the
failure was only a warning), and ``SELECT 1 AS demo_col`` stood in for a build
with no SQL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build._errors import FluidUserError
from fluid_build.cli import build_parser
from fluid_build.cli.apply import run as apply_run
from fluid_build.providers.local.local import LocalProvider

pytestmark = [pytest.mark.integration]

LOG = logging.getLogger("test.local_no_placeholder")


def _contract(**build: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "demo.no_placeholder",
        "name": "No placeholder",
        "domain": "demo",
        "metadata": {"layer": "Bronze", "owner": {"team": "demo-team"}},
        "builds": [
            {
                "id": "make",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1 AS a"},
                "outputs": ["A"],
                **build,
            }
        ],
        "exposes": [
            {
                "exposeId": "A",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "out/A.csv"},
                },
                "contract": {"schema": [{"name": "a", "type": "integer"}]},
            }
        ],
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("FLUID_ENV", "FLUID_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _apply(project: Path, contract: Dict[str, Any], *flags: str) -> int:
    (project / "contract.fluid.yaml").write_text(yaml.safe_dump(contract, sort_keys=False), "utf-8")
    args = build_parser().parse_args(["apply", "contract.fluid.yaml", *flags, "--yes"])
    return apply_run(args, LOG)


def _files(project: Path) -> list:
    return sorted(p.name for p in (project / "out").glob("*")) if (project / "out").is_dir() else []


def test_a_plan_that_fails_validation_exits_with_a_named_event_and_writes_nothing(
    project: Path,
) -> None:
    # An expose no build writes: ``materialize missing source_table``.
    contract = _contract()
    contract["builds"][0]["outputs"] = ["A"]
    contract["exposes"].append(
        dict(
            contract["exposes"][0],
            exposeId="B",
            binding={
                "platform": "local",
                "format": "csv",
                "location": {"path": "out/B.csv"},
            },
        )
    )
    with pytest.raises(FluidUserError) as refused:
        _apply(project, contract)
    assert refused.value.code == "local_plan_invalid"
    assert "B: materialize missing source_table" in refused.value.why
    assert _files(project) == []  # not even the expose that resolves: nothing was written


def test_a_build_with_no_sql_is_refused_not_answered_with_a_demo_row(project: Path) -> None:
    contract = _contract()
    contract["builds"][0].update(pattern="multi-stage", properties={"stages": []})
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    with pytest.raises(FluidUserError) as refused:
        provider._derive_actions_from_contract(contract)
    assert refused.value.code == "build_has_no_sql"


def test_a_result_table_that_cannot_be_created_fails_the_apply(project: Path) -> None:
    # An invalid table name used to be a warning (local_sql_create_table_warn) and
    # the expose that reads the table got the placeholder.
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    result = provider.apply(
        actions=[
            {
                "op": "execute_sql",
                "sql": "SELECT 1 AS a",
                "table_name": "result_bad-name",
                "output_table": "result_bad-name",
            },
            {
                "op": "materialize",
                "path": str(project / "out" / "A.csv"),
                "source_table": "result_bad-name",
            },
        ]
    )
    assert result["failed"] == 2 and result["applied"] == 0
    assert "Invalid SQL identifier" in result["results"][0]["error"]
    assert _files(project) == []


def test_apply_with_nothing_to_run_is_an_error_not_a_demo_artifact(project: Path) -> None:
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    with pytest.raises(ValueError):
        provider.apply()
    assert not (project / "runtime" / "out" / "demo_artifact.csv").exists()


def test_a_materialize_with_no_source_fails_and_leaves_an_existing_file_alone(
    project: Path,
) -> None:
    target = project / "out" / "A.csv"
    target.parent.mkdir()
    target.write_text("a\n41\n", "utf-8")
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    result = provider.apply(actions=[{"op": "materialize", "path": str(target)}])
    assert result["failed"] == 1 and result["applied"] == 0
    assert target.read_text("utf-8") == "a\n41\n"


def test_a_materialize_a_build_runner_lands_is_skipped_not_applied(project: Path) -> None:
    target = project / "out" / "A.csv"
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    result = provider.apply(
        actions=[{"op": "materialize", "path": str(target), "landed_by": ["ingest"]}]
    )
    assert (result["applied"], result["failed"]) == (0, 0)
    assert result["results"][0]["skipped"] is True
    assert not target.exists()
