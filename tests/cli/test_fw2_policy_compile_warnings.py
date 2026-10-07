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

"""``fluid policy compile`` shows its warnings, and fails on a compiler crash.

RT-707-3: a grant on an Iceberg table in a catalog the compiler cannot grant
on (``catalog: lakekeeper`` on AWS) compiles to no table grant and a warning.
The warning went only into ``bindings.json``; the console got an INFO count
that is routed to DEBUG, so the command printed nothing and exited 0, and
``fluid policy apply`` never read the warnings either. A crash inside
``compile_policy`` was swallowed into a bindings file and exit 0 as well.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import policy_apply, policy_compile
from fluid_build.cli._common import CLIError

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]

_LAKEKEEPER_GRANT: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "analytics.pol",
    "name": "Pol",
    "metadata": {"owner": {"team": "data"}, "layer": "Bronze"},
    "accessPolicy": {
        "grants": [{"principal": "group:analysts@example.com", "permissions": ["read"]}]
    },
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "catalog": "lakekeeper",
                    "uri": "http://lakekeeper:8181/catalog",
                    "warehouse": "analytics",
                    "database": "streaming",
                    "table": "orders",
                    "bucket": "acme-lake",
                    "region": "eu-west-1",
                },
            },
            "contract": {"schema": [{"name": "order_id", "type": "integer", "required": True}]},
        }
    ],
}

_NO_TABLE_GRANT = "so no table grant was compiled for group:analysts@example.com"


def _write_contract(tmp_path: Path, doc: Dict[str, Any]) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _warning_events(caplog, name: str) -> List[Dict[str, Any]]:
    out = []
    for record in caplog.records:
        if record.levelno != logging.WARNING:
            continue
        try:
            doc = json.loads(record.getMessage())
        except ValueError:
            continue
        if doc.get("message") == name:
            out.append(doc)
    return out


def test_compile_shows_each_warning_at_warning_level_and_exits_0(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="test.fw2.policy")
    out = tmp_path / "policy" / "bindings.json"
    args = argparse.Namespace(
        contract=str(_write_contract(tmp_path, _LAKEKEEPER_GRANT)), out=str(out)
    )

    assert policy_compile.run(args, logging.getLogger("test.fw2.policy")) == 0

    stored = json.loads(out.read_text())["warnings"]
    shown = _warning_events(caplog, "policy_compile_warning")
    assert [e["warning"] for e in shown] == stored
    assert any(_NO_TABLE_GRANT in w for w in stored)


def test_a_compiler_crash_fails_and_writes_no_bindings(tmp_path, monkeypatch):
    def _boom(contract):
        raise KeyError("location")

    monkeypatch.setattr("fluid_build.policy.compiler.compile_policy", _boom)
    out = tmp_path / "bindings.json"
    args = argparse.Namespace(
        contract=str(_write_contract(tmp_path, _LAKEKEEPER_GRANT)), out=str(out)
    )

    with pytest.raises(CLIError) as excinfo:
        policy_compile.run(args, logging.getLogger("test.fw2.policy"))

    assert excinfo.value.exit_code == 1
    assert excinfo.value.event == "policy_compiler_crashed"
    assert "policy compiler failed: KeyError" in excinfo.value.context["error"]
    assert not out.exists()


def test_a_compiler_crash_leaves_an_earlier_bindings_file_as_it_was(tmp_path, monkeypatch):
    out = tmp_path / "bindings.json"
    out.write_text('{"bindings": [], "warnings": ["earlier"]}', encoding="utf-8")

    def _boom(contract):
        raise RuntimeError("compiler bug")

    monkeypatch.setattr("fluid_build.policy.compiler.compile_policy", _boom)
    args = argparse.Namespace(
        contract=str(_write_contract(tmp_path, _LAKEKEEPER_GRANT)), out=str(out)
    )

    with pytest.raises(CLIError):
        policy_compile.run(args, logging.getLogger("test.fw2.policy"))
    assert out.read_text(encoding="utf-8") == '{"bindings": [], "warnings": ["earlier"]}'


def test_apply_echoes_the_stored_warnings(tmp_path, monkeypatch, caplog):
    """An empty bindings list is the no-op path, which resolves no provider;
    the warnings are echoed before it."""
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.DEBUG, logger="test.fw2.policy")
    warning = "Iceberg expose 'orders' is cataloged in 'lakekeeper', not AWS Glue, " + (
        _NO_TABLE_GRANT
    )
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({"bindings": [], "warnings": [warning]}), encoding="utf-8")
    args = argparse.Namespace(bindings="bindings.json", mode="check", provider=None, project=None)

    assert policy_apply.run(args, logging.getLogger("test.fw2.policy")) == 0

    shown = _warning_events(caplog, "policy_bindings_warning")
    assert [e["warning"] for e in shown] == [warning]


def _cli(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(_REPO), "NO_COLOR": "1"}
    return subprocess.run(
        [sys.executable, "-m", "fluid_build", *argv],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.integration
def test_the_cli_prints_the_warning_on_stderr(tmp_path):
    """The real entry point, as the defect was measured: the warning reaches
    stderr (stdout stays the command's own output) and the exit stays 0."""
    contract = _write_contract(tmp_path, _LAKEKEEPER_GRANT)
    result = _cli(tmp_path, "policy", "compile", contract.name, "--out", "out/b.json")

    assert result.returncode == 0, result.stderr
    assert _NO_TABLE_GRANT in result.stderr
    assert _NO_TABLE_GRANT not in result.stdout


@pytest.mark.integration
def test_the_cli_fails_on_a_compiler_crash(tmp_path):
    """A grant that is a bare string crashes ``compile_policy``; that used to
    write ``{"bindings": {"bindings": []}}`` and exit 0."""
    doc = json.loads(json.dumps(_LAKEKEEPER_GRANT))
    doc["accessPolicy"]["grants"] = ["group:analysts@example.com"]
    contract = _write_contract(tmp_path, doc)
    result = _cli(tmp_path, "policy", "compile", contract.name, "--out", "out/b.json")

    assert result.returncode == 1
    assert "ERR_POLICY_COMPILER_CRASHED" in result.stdout + result.stderr
    assert not (tmp_path / "out" / "b.json").exists()
