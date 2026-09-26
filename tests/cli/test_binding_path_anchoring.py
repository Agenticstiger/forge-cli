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

"""One anchoring rule for a relative ``binding.location.path``.

It resolves against the directory of the SOURCE contract file, whichever
stage writes or reads it and whichever directory that stage was launched
from. Before, the acquisition runner anchored at the contract directory,
the local provider (every embedded-SQL build and every native local apply)
wrote relative to the working directory, ``fluid verify`` read relative to
the working directory, and a build planned from a bundle anchored at the
bundle's directory. So a pipeline running from the repo root verified a
path the build never wrote.

Every test launches from the workspace root with the contract two levels
down, which is the layout that exposed it.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Callable, List

import pytest

from fluid_build.cli import apply as apply_cmd
from fluid_build.cli import bundle as bundle_cmd
from fluid_build.cli import plan as plan_cmd
from fluid_build.cli import verify as verify_cmd

duckdb = pytest.importorskip("duckdb")

LOG = logging.getLogger("test.binding_path_anchoring")

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.gates
name: Gates Demo
description: Inline SQL build.
domain: Demo
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
builds:
  - id: make_rows
    description: Inline rows.
    pattern: embedded-logic
    engine: sql
    properties:
      sql: "SELECT 1 AS id, 'a' AS name"
    outputs: [rows]
exposes:
  - exposeId: rows
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/rows.parquet
    contract:
      schema:
        - {name: id, type: INTEGER, required: true}
        - {name: name, type: VARCHAR, required: false}
"""

_C = "contracts/p/contract.fluid.yaml"


def _parse(register: Callable[[Any], None], argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fluid")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--region", default=None)
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser.parse_args(argv)


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    (tmp_path / "contracts" / "p").mkdir(parents=True)
    (tmp_path / _C).write_text(_CONTRACT, encoding="utf-8")
    (tmp_path / "runtime").mkdir()
    return tmp_path


def _landed(ws: Path) -> List[str]:
    return sorted(str(p.relative_to(ws)) for p in ws.rglob("rows.parquet"))


def _apply(argv: List[str]) -> int:
    return apply_cmd.run(_parse(apply_cmd.register, ["apply", *argv, "--yes"]), LOG)


class TestWritersAnchorAtTheContract:
    def test_embedded_sql_build_from_a_contract(self, ws: Path) -> None:
        assert _apply([_C, "--mode", "amend-and-build"]) == 0
        assert _landed(ws) == ["contracts/p/out/rows.parquet"]

    def test_native_local_apply(self, ws: Path) -> None:
        assert _apply([_C, "--mode", "amend"]) == 0
        assert _landed(ws) == ["contracts/p/out/rows.parquet"]

    def test_build_planned_from_a_bundle(self, ws: Path) -> None:
        bundle = ["bundle", _C, "--format", "tgz", "--out", "runtime/p.tgz"]
        assert bundle_cmd.run(_parse(bundle_cmd.register, bundle), LOG) == 0
        plan = ["plan", "runtime/p.tgz", "--mode", "amend-and-build", "--out", "runtime/plan.json"]
        assert plan_cmd.run(_parse(plan_cmd.register, plan), LOG) == 0
        rc = _apply(["runtime/plan.json", "--bundle", "runtime/p.tgz", "--mode", "amend-and-build"])
        assert rc == 0
        assert _landed(ws) == ["contracts/p/out/rows.parquet"]


class TestVerifyReadsWhereTheWriterWrote:
    def _write_rows(self, ws: Path) -> None:
        out = ws / "contracts" / "p" / "out"
        out.mkdir(parents=True, exist_ok=True)
        duckdb.sql(
            f"COPY (SELECT 1 AS id, 'a' AS name) TO '{out / 'rows.parquet'}' (FORMAT parquet)"
        )

    def _verify(self, src: str, report: str) -> dict:
        args = _parse(verify_cmd.register, ["verify", src, "--strict", "--out", report])
        assert verify_cmd.run(args, LOG) == 0
        return json.loads(Path(report).read_text(encoding="utf-8"))["results"]["rows"]

    def test_verify_from_the_workspace_root(self, ws: Path) -> None:
        self._write_rows(ws)
        result = self._verify(_C, "runtime/verify.json")
        assert result["status"] == "match"
        assert Path(result["path"]) == (ws / "contracts" / "p" / "out" / "rows.parquet")

    def test_verify_a_bundle_reads_next_to_its_source_contract(self, ws: Path) -> None:
        bundle = ["bundle", _C, "--format", "tgz", "--out", "runtime/p.tgz"]
        assert bundle_cmd.run(_parse(bundle_cmd.register, bundle), LOG) == 0
        self._write_rows(ws)
        result = self._verify("runtime/p.tgz", "runtime/verify.json")
        assert result["status"] == "match"
        assert result["row_count"] == 1


class TestNoPlaceholderOverBuiltData:
    """With one anchor, a native ``amend`` and the build write the SAME file.

    ``amend`` on a contract with nothing to run locally used to write a
    placeholder (``SELECT 1 AS demo_col`` / ``id,value``) to the declared
    path. It must never replace data a build already landed there.
    """

    # An acquisition build (the demo's shape): the build lands the file, and
    # the local provider has no SQL to run for it.
    _NO_SQL = _CONTRACT.replace(
        """  - id: make_rows
    description: Inline rows.
    pattern: embedded-logic
    engine: sql
    properties:
      sql: "SELECT 1 AS id, 'a' AS name"
    outputs: [rows]
""",
        """  - id: ingest_rows
    description: Full refresh from the source.
    pattern: acquisition
    engine: duckdb
    capabilities: [full_refresh]
    properties:
      source:
        kind: postgres
        connection: {host: localhost, port: "5432", database: src, user: reader}
        mode: full_refresh
        streams: [public.rows]
      sink: {format: parquet}
    outputs: [rows]
""",
    )

    def test_amend_leaves_an_existing_output_alone(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (ws / _C).write_text(self._NO_SQL, encoding="utf-8")
        out = ws / "contracts" / "p" / "out"
        out.mkdir(parents=True)
        duckdb.sql(
            f"COPY (SELECT 7 AS id, 'built' AS name) TO '{out / 'rows.parquet'}' (FORMAT parquet)"
        )
        # Launch from the contract's own directory: the one layout where the
        # old working-directory rule and the contract-directory rule agree,
        # so the placeholder would have hit the built file on either.
        monkeypatch.chdir(ws / "contracts" / "p")
        apply_cmd.run(
            _parse(
                apply_cmd.register, ["apply", "contract.fluid.yaml", "--mode", "amend", "--yes"]
            ),
            LOG,
        )
        rows = duckdb.sql(f"SELECT * FROM '{out / 'rows.parquet'}'").fetchall()
        assert rows == [(7, "built")]
