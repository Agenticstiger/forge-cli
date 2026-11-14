# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""`fluid mcp output-port serve` refuses a tool name that names no tool.

A mistyped ``--deny-tools`` name used to deny nothing and leave the tool
callable; a mistyped ``--allow-tools`` name served fewer tools without a word.
`fluid mcp serve` has refused both since #884; the output port now does too, at
startup and before either transport binds.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import mcp_output_port as cli_module

_CONTRACT = """
fluidVersion: "0.7.4"
kind: DataProduct
id: demo.local.tool_name_check
name: Tool name check
domain: demo
metadata:
  layer: Bronze
  owner: {{team: x, email: x@example.com}}
exposes:
  - exposeId: rows
    kind: table
    binding:
      platform: local
      format: csv
      location: {{path: "{csv}", table: rows}}
    contract:
      schema:
        - name: a
          type: STRING
          required: true
"""


@pytest.fixture
def serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    pytest.importorskip("mcp")
    monkeypatch.setenv("FLUID_AUDIT_ROOT", str(tmp_path / "audit"))
    monkeypatch.setenv("FLUID_MCP_AUTH_MODE", "shared-token")
    monkeypatch.setenv("FLUID_MCP_AUTH_TOKEN", "t" * 32)
    csv = tmp_path / "rows.csv"
    csv.write_text("a\n1\n", encoding="utf-8")
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(_CONTRACT.format(csv=csv), encoding="utf-8")
    started: List[Dict[str, Any]] = []

    def _fake_run_stdio(**kwargs: Any) -> int:
        started.append(kwargs)
        return 0

    monkeypatch.setattr("fluid_build.output_ports.mcp.run_stdio", _fake_run_stdio)

    def _serve(transport: str = "stdio", **flags: Any):
        values = {
            "contract": str(contract),
            "env": None,
            "expose_id": None,
            "allow_tools": None,
            "deny_tools": None,
            "readable_paths": None,
            "allow_sql": False,
            "max_sample_rows": 100,
            "query_timeout_seconds": 60.0,
            "transport": transport,
            "host": "127.0.0.1",
            "port": 8765,
            "allow_models": None,
            "deny_models": None,
            "allow_use_cases": None,
            "deny_use_cases": None,
        }
        values.update(flags)
        args = type("Args", (), values)()
        rc = cli_module._run_serve(args, logging.getLogger("test"))
        return rc, capsys.readouterr().err, started

    return _serve


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_an_unknown_deny_name_refuses_before_binding(serve, transport: str) -> None:
    rc, err, started = serve(transport, deny_tools="no_such_tool")
    assert rc == 2
    assert "fluid mcp output-port serve: --deny-tools names no such tool: no_such_tool." in err
    assert "  Tools: " in err and "describe" in err
    assert started == []


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_an_unknown_allow_name_refuses_before_binding(serve, transport: str) -> None:
    rc, err, started = serve(transport, allow_tools="no_such_tool")
    assert rc == 2
    assert "--allow-tools names no such tool: no_such_tool." in err
    assert started == []


def test_only_the_unknown_names_are_named(serve) -> None:
    rc, err, started = serve(allow_tools="describe,sampel", deny_tools="sample, nope ,query")
    assert rc == 2
    assert "--allow-tools names no such tool: sampel." in err
    assert "--deny-tools names no such tool: nope." in err
    tools_line = err.strip().splitlines()[-1]
    names = tools_line.removeprefix("  Tools: ").rstrip(".").split(", ")
    assert names == sorted(names) and "describe" in names
    assert started == []


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_known_names_start_as_before(serve, transport: str) -> None:
    rc, err, started = serve(transport, allow_tools="describe,sample", deny_tools="sample")
    assert rc == 0
    assert "no such tool" not in err
    assert len(started) == 1
    policy = started[0]["policy"]
    assert policy.allowed_tools == ("describe", "sample")
    assert policy.denied_tools == ("sample",)


def test_no_flags_and_empty_values_mean_all_tools(serve) -> None:
    for flags in ({}, {"allow_tools": "", "deny_tools": ""}):
        rc, _, started = serve(**flags)
        assert rc == 0
        assert started[-1]["policy"].allowed_tools is None
        assert started[-1]["policy"].denied_tools == ()
