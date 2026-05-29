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

"""End-to-end tests for `fluid mcp output-port list` and `... doctor`.

Spawns the CLI as a subprocess to lock in the surfaces operators
actually run before plugging the server into an MCP client."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")


def _make_demo(tmp_path: Path) -> Path:
    csv_path = tmp_path / "customers.csv"
    csv_path.write_text(
        "customer_id,segment\n" "C0001,enterprise\n" "C0002,smb\n" "C0003,consumer\n",
        encoding="utf-8",
    )
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text(
        textwrap.dedent(
            """\
            fluidVersion: "0.7.3"
            kind: DataProduct
            id: silver.demo.customer_segments_v1
            name: Customer Segments
            metadata:
              layer: Silver
              owner: { team: demo, email: demo@example.com }
              businessContext: { domain: Demo }
            exposes:
              - exposeId: customer_segments
                kind: table
                title: Customer Segments
                contract:
                  schema:
                    - { name: customer_id, type: STRING, required: true }
                    - { name: segment, type: STRING }
                binding:
                  platform: local
                  format: csv
                  location: { path: ./customers.csv, table: customer_segments }
                semantics:
                  name: customer_segments
                  measures:
                    - { name: customer_count, agg: count, expr: customer_id }
                  dimensions:
                    - { name: segment, type: categorical }
                  metrics:
                    - { name: active_customers, type: simple, measure: customer_count }
            """
        ),
        encoding="utf-8",
    )
    return contract_path


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["FLUID_QUIET"] = "1"
    env["FLUID_NONINTERACTIVE"] = "1"
    # Strip debug-level env vars that the cli/conftest sets globally —
    # they enable the registration banner on stdout and contaminate
    # the JSON output the doctor / list subcommands emit.
    for noisy_var in ("FLUID_LOG_LEVEL", "FLUID_DEBUG", "FLUID_SHOW_REGISTRATION"):
        env.pop(noisy_var, None)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _parse_json_payload(stdout: str) -> dict:
    """Extract the JSON document from CLI stdout, tolerating leading
    banner / log lines that other tests' env-var leakage may have
    enabled. The CLI's JSON output is a single self-contained
    object emitted at the end of the stream."""
    candidate = stdout.strip()
    if candidate.startswith("{") or candidate.startswith("["):
        return json.loads(candidate)
    # Find the first balanced JSON object by scanning forward to the
    # last `{` that has a matching depth-balanced `}`.
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise AssertionError(f"no JSON in stdout:\n{stdout!r}")
    return json.loads(stdout[start : end + 1])


def test_list_human_output(tmp_path: Path):
    contract = _make_demo(tmp_path)
    proc = _run_cli(
        ["mcp", "output-port", "list", str(contract)],
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "customer_segments" in proc.stdout
    assert "local/csv" in proc.stdout
    assert "describe, sample, query" in proc.stdout
    assert "semantics" in proc.stdout


def test_list_json_output(tmp_path: Path):
    contract = _make_demo(tmp_path)
    proc = _run_cli(
        ["mcp", "output-port", "list", "--json", str(contract)],
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    payload = _parse_json_payload(proc.stdout)
    assert len(payload["exposes"]) == 1
    entry = payload["exposes"][0]
    assert entry["exposeId"] == "customer_segments"
    assert entry["hasSemantics"] is True
    assert entry["hasMcpOverrides"] is False


def test_doctor_green_against_local_csv(tmp_path: Path):
    contract = _make_demo(tmp_path)
    proc = _run_cli(
        ["mcp", "output-port", "doctor", str(contract)],
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "driver_load: duckdb" in proc.stdout
    assert "engine_health: duckdb-ok" in proc.stdout


def test_doctor_json_green(tmp_path: Path):
    contract = _make_demo(tmp_path)
    proc = _run_cli(
        ["mcp", "output-port", "doctor", "--json", str(contract)],
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    report = _parse_json_payload(proc.stdout)
    assert report["status"] == "ok"
    assert report["exposeId"] == "customer_segments"
    assert any(c["name"] == "driver_load" for c in report["checks"])


def test_doctor_fails_on_missing_table_with_helpful_detail(tmp_path: Path):
    csv_path = tmp_path / "customers.csv"
    csv_path.write_text("customer_id\nC1\n", encoding="utf-8")
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(
        textwrap.dedent(
            """\
            fluidVersion: "0.7.3"
            kind: DataProduct
            id: silver.demo.bad_path_v1
            name: Bad Path
            metadata:
              layer: Silver
              owner: { team: demo, email: demo@example.com }
            exposes:
              - exposeId: missing
                kind: table
                contract:
                  schema:
                    - { name: id, type: STRING, required: true }
                binding:
                  platform: local
                  format: csv
                  location:
                    path: /this/path/does/not/exist.csv
                    table: missing
            """
        ),
        encoding="utf-8",
    )
    proc = _run_cli(
        ["mcp", "output-port", "doctor", "--json", str(contract)],
        cwd=tmp_path,
    )
    assert proc.returncode == 1, proc.stdout
    report = _parse_json_payload(proc.stdout)
    assert report["status"] == "fail"
    failing = [c for c in report["checks"] if c["status"] == "fail"]
    assert failing, report


def test_serve_auto_picks_when_single_expose(tmp_path: Path):
    """Confirms the CLI no longer requires --expose-id when the
    contract has one expose. The auto-pick stderr line is the
    operator-visible signal."""
    contract = _make_demo(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build",
            "mcp",
            "output-port",
            "serve",
            str(contract),
        ],
        input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
        cwd=str(tmp_path),
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "FLUID_QUIET": "1",
            "FLUID_NONINTERACTIVE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "auto-selected expose" in proc.stderr
    assert "customer_segments" in proc.stderr


def test_serve_multi_expose_without_id_returns_helpful_error(tmp_path: Path):
    csv_path = tmp_path / "customers.csv"
    csv_path.write_text("customer_id\nC1\n", encoding="utf-8")
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(
        textwrap.dedent(
            """\
            fluidVersion: "0.7.3"
            kind: DataProduct
            id: silver.demo.multi_v1
            name: Multi
            metadata:
              layer: Silver
              owner: { team: demo, email: demo@example.com }
            exposes:
              - exposeId: a
                kind: table
                contract: { schema: [{ name: customer_id, type: STRING, required: true }] }
                binding: { platform: local, format: csv, location: { path: ./customers.csv, table: a } }
              - exposeId: b
                kind: table
                contract: { schema: [{ name: customer_id, type: STRING, required: true }] }
                binding: { platform: local, format: csv, location: { path: ./customers.csv, table: b } }
            """
        ),
        encoding="utf-8",
    )
    proc = _run_cli(
        ["mcp", "output-port", "serve", str(contract)],
        cwd=tmp_path,
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "2 exposes" in combined or "pass --expose-id" in combined
    assert "'a'" in combined and "'b'" in combined
