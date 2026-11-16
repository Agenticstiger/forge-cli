# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid spark run|status``: parsing, exit codes and JSON, without any AWS call."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fluid_build.build_runners.spark._reconcile import Outcome
from fluid_build.cli import build_parser
from fluid_build.cli import spark as cli_spark
from fluid_build.cli._common import CLIError
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.spark.conftest import CONTRACT

LOG = logging.getLogger("test")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example,example.internal")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")


@pytest.fixture
def contract_file(tmp_path):
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(CONTRACT))
    return str(path)


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_the_parser_takes_run_and_status():
    args = parse("spark", "run", "c.yaml", "--build-id", "x", "--no-wait", "--json")
    assert (args.build_id, args.no_wait, args.json, args.func) == (
        "x",
        True,
        True,
        cli_spark.run_run,
    )
    args = parse("spark", "status", "c.yaml")
    assert (args.func, args.build_id) == (cli_spark.run_status, None)
    with pytest.raises(SystemExit):
        parse("spark", "run", "c.yaml")  # --build-id is required


def _outcome(**kw):
    return Outcome("ingest-orders", "emr-serverless", **kw)


def _patch_run_once(monkeypatch, outcome):
    monkeypatch.setattr(
        "fluid_build.build_runners.spark._reconcile.run_once", lambda run, **kw: outcome
    )


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (_outcome(action="succeeded", state="SUCCEEDED"), 0),
        (_outcome(action="failed", error=True, message="FAILED"), 1),
        (_outcome(action="failed", error=True, message="cancelled"), 1),
    ],
)
def test_run_returns_the_outcomes_exit_code(monkeypatch, contract_file, outcome, code, capsys):
    _patch_run_once(monkeypatch, outcome)
    args = parse("spark", "run", contract_file, "--build-id", "ingest-orders")
    assert args.func(args, LOG) == code
    assert "ingest-orders" in capsys.readouterr().out


def test_run_json_prints_one_json_line(monkeypatch, contract_file, capsys):
    _patch_run_once(monkeypatch, _outcome(action="failed", error=True, message="boom"))
    args = parse("spark", "run", contract_file, "--build-id", "ingest-orders", "--json")
    assert args.func(args, LOG) == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert (payload["action"], payload["error"], payload["build"]) == (
        "failed",
        True,
        "ingest-orders",
    )


def test_an_unknown_build_is_a_refusal_with_exit_code_two(contract_file):
    args = parse("spark", "run", contract_file, "--build-id", "nope")
    with pytest.raises(CLIError) as err:
        args.func(args, LOG)
    assert (err.value.event, err.value.exit_code) == ("spark_run_no_build", 2)


def test_a_policy_refusal_is_exit_code_two(monkeypatch, contract_file):
    monkeypatch.delenv(HOST_ALLOWLIST_ENV)
    args = parse("spark", "run", contract_file, "--build-id", "ingest-orders")
    with pytest.raises(CLIError) as err:
        args.func(args, LOG)
    assert (err.value.event, err.value.exit_code) == ("spark_run_refused", 2)


@pytest.mark.parametrize("build_id", ["nope", "ingest-orders"])
def test_json_refusals_are_one_line_and_exit_two_without_raising(
    monkeypatch, contract_file, capsys, build_id
):
    if build_id == "ingest-orders":
        monkeypatch.delenv(HOST_ALLOWLIST_ENV)
    args = parse("spark", "run", contract_file, "--build-id", build_id, "--json")
    assert args.func(args, LOG) == 2
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert (payload["action"], payload["error"]) == ("refused", True)
    assert payload["event"].startswith("spark_run_")


def test_status_prints_rows(monkeypatch, contract_file, capsys):
    rows = [{"build": "ingest-orders", "runtime": "emr-serverless", "where": "app", "runs": []}]
    monkeypatch.setattr("fluid_build.build_runners.spark._reconcile.status", lambda runs: rows)
    args = parse("spark", "status", contract_file, "--json")
    assert args.func(args, LOG) == 0
    assert json.loads(capsys.readouterr().out)[0]["build"] == "ingest-orders"


def test_importing_the_command_keeps_boto3_and_the_runner_off_the_cold_path():
    code = (
        "import sys, fluid_build.cli.spark;"
        "bad = [m for m in ('boto3', 'fluid_build.build_runners.spark') if m in sys.modules];"
        "print(bad)"
    )
    root = str(Path(cli_spark.__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": root}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    assert out.stdout.strip() == "[]"
