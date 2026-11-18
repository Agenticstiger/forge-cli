# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""runtime: local through fluid plan and fluid apply: the native engine runs the Spark
hooks for it, the local provider leaves its build and output to the Spark runner, AWS
emits nothing for it, and the operator's settings stay out of a project's dotenv."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
import yaml

from fluid_build.build_runners.spark._local import SUBMIT_EXECUTABLE_ENV
from fluid_build.cli import _spark_apply as _apply
from fluid_build.cli._common import CLIError
from tests.build_runners.spark.test_local import STUB

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="local runs are POSIX only")

LOG = logging.getLogger("test")
PRODUCT = "platform.events"


def contract(*builds):
    """The probe contract's daily batch, on this machine: raw.events into curated."""
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Events",
        "metadata": {"owner": {"team": "data-platform"}},
        "builds": list(builds) or [daily()],
        "exposes": [
            {
                "exposeId": "events_daily",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "iceberg",
                    "location": {
                        "catalog": "lakekeeper",
                        "database": "curated",
                        "table": "events_daily",
                        "bucket": "lake",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "event_date", "type": "date", "required": True},
                        {"name": "events", "type": "bigint"},
                    ]
                },
            }
        ],
    }


def daily(runtime="local", **spark):
    return {
        "id": "daily-curated",
        "engine": "spark",
        "pattern": "embedded-logic",
        "outputs": ["events_daily"],
        "execution": {"trigger": {"type": "schedule", "schedule": "0 2 * * *"}},
        "properties": {
            "sql": "SELECT CAST(event_time AS DATE) AS event_date, COUNT(*) AS events "
            "FROM raw.events GROUP BY CAST(event_time AS DATE)",
            "parameters": {"spark": {"runtime": runtime, "mode": "batch", **spark}},
        },
    }


@pytest.fixture(autouse=True)
def operator(monkeypatch, tmp_path):
    from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV

    stub = tmp_path / "bin" / "spark-submit"
    stub.parent.mkdir()
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, PRODUCT)
    monkeypatch.setenv(SUBMIT_EXECUTABLE_ENV, str(stub))
    monkeypatch.delenv("FLUID_SPARK_LOCAL_ROOT", raising=False)
    monkeypatch.delenv("FLUID_SPARK_SUBMIT_PROPERTIES_FILE", raising=False)
    return stub


def test_the_native_engine_takes_a_local_build_and_refuses_an_aws_one():
    _apply.refuse_without_opentofu(contract(), "native")
    aws = dict(daily("emr-serverless"), id="on-aws")
    with pytest.raises(CLIError) as caught:
        _apply.refuse_without_opentofu(contract(daily(), aws), "native")
    assert caught.value.event == "apply_spark_needs_opentofu"
    assert "on-aws" in str(caught.value.context)


def test_prepare_refuses_a_local_run_this_machine_cannot_start(monkeypatch):
    monkeypatch.setenv(SUBMIT_EXECUTABLE_ENV, "/nonexistent/spark-submit")
    with pytest.raises(CLIError) as caught:
        _apply.prepare(contract(), build_id=None, verified_plan=None, dry_run=False)
    assert caught.value.event == "apply_spark_local_unavailable"
    assert "daily-curated" in caught.value.context["error"]
    # A dry run starts nothing, so it checks nothing on this machine.
    (run,) = _apply.prepare(contract(), build_id=None, verified_plan=None, dry_run=True)
    assert run.local


def test_prepare_checks_a_local_run_against_the_contract_s_directory(tmp_path, monkeypatch):
    from fluid_build.build_runners.spark import _local

    seen = []
    real = _local.default_host

    def default_host(workspace=None):
        seen.append(workspace)
        return real(workspace)

    monkeypatch.setattr(_local, "default_host", default_host)
    _apply.prepare(contract(), build_id=None, verified_plan=None, dry_run=False, workspace=tmp_path)
    assert seen == [tmp_path]


def test_plan_lists_a_local_reconcile_action_apply_checks_it_against():
    (action,) = _apply.planned_actions(contract())
    assert (action["provider"], action["payload"]["runtime"]) == ("local", "local")
    plan = {"actions": [{"step": 1, "depends_on": [], **action}]}
    (run,) = _apply.prepare(contract(), build_id=None, verified_plan=plan, dry_run=True)
    assert run.build == "daily-curated"


def test_reconcile_after_apply_with_nothing_here_does_nothing(tmp_path):
    rc = _apply.reconcile_after_apply(
        [], LOG, dry_run=False, workspace=tmp_path, contract=contract(), build_id=None
    )
    assert rc == 0 and not (tmp_path / ".fluid").exists()


def write(tmp_path, data, name="contract.fluid.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_fluid_apply_runs_a_local_batch_build_on_the_native_engine(tmp_path, capsys):
    from fluid_build.cli import main

    path = write(tmp_path, contract())
    code = main(
        [
            "apply",
            str(path),
            "--mode",
            "amend-and-build",
            "--build-id",
            "daily-curated",
            "--yes",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Spark daily-curated (local): succeeded run" in out
    runs = tmp_path / ".fluid" / PRODUCT / "runtime" / "spark" / "daily-curated" / "runs"
    assert len(list(runs.iterdir())) == 1
    layout = tmp_path / ".fluid" / "spark-artifacts" / "_fluid" / "spark" / PRODUCT
    assert (layout / "daily-curated" / "job.json").is_file()


def test_fluid_apply_dry_run_starts_nothing(tmp_path, capsys):
    from fluid_build.cli import main

    path = write(tmp_path, contract())
    code = main(["apply", str(path), "--mode", "amend-and-build", "--dry-run", "--yes"])
    assert code == 0, capsys.readouterr().out
    assert not (tmp_path / ".fluid" / PRODUCT).exists()


def test_the_local_planner_leaves_a_spark_build_and_its_output_to_the_runner():
    from fluid_build.providers.local.planner import plan_actions

    actions = plan_actions(contract())
    assert not [a for a in actions if a.get("resource_id") in ("daily-curated", "events_daily")]


def test_aws_emits_nothing_for_a_local_build():
    from fluid_build.iac.providers.aws_ingest import spark_blocks

    blocks = spark_blocks(contract(), "c")
    assert (blocks.resources, blocks.data) == ({}, {})


def test_a_local_batch_needs_no_aws_role_and_its_inert_role_is_not_checked():
    from fluid_build.iac.providers import aws_identity

    build = daily(executionRoleArn="arn:aws:iam::123456789012:role/unlisted")
    assert aws_identity.runtime_errors(daily()) == []
    assert aws_identity.identity_findings(build) == ([], [])
    assert aws_identity.validation_errors(contract(build)) == []


def test_a_denied_spark_setting_is_still_refused_on_a_local_build():
    from fluid_build.iac.providers import aws_identity

    build = daily(sparkConf={"spark.jars": "/tmp/evil.jar"})
    errors, _ = aws_identity.identity_findings(build)
    assert errors and "spark.jars" in errors[0]


def test_the_build_phase_hands_the_spark_runner_the_contract_s_directory(monkeypatch, tmp_path):
    from fluid_build.build_runners import base
    from fluid_build.build_runners.spark import _reconcile

    seen = {}

    def run_build(contract, build, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(_reconcile, "run_build", run_build)
    assert base._execute_spark_build(contract(), daily(), dry_run=False, workspace=tmp_path) == 0
    assert Path(seen["local_host"].workspace) == tmp_path


@pytest.mark.parametrize(
    "name",
    [
        "FLUID_SPARK_SUBMIT_EXECUTABLE",
        "FLUID_SPARK_SUBMIT_PROPERTIES_FILE",
        "FLUID_SPARK_LOCAL_ROOT",
        "SPARK_HOME",
        "SPARK_CONF_DIR",
        "SPARK_SUBMIT_OPTS",
        "PYSPARK_PYTHON",
        "PYSPARK_DRIVER_PYTHON",
        "HADOOP_CONF_DIR",
        "YARN_CONF_DIR",
    ],
)
def test_a_project_dotenv_cannot_choose_what_a_local_run_starts(name):
    from fluid_build.credentials.protected_env import is_protected_env_key

    assert is_protected_env_key(name)


def test_both_redaction_layers_mask_a_rest_catalog_credential():
    from fluid_build.observability.secret_redactor import redact_secret_text
    from fluid_build.providers.snowflake.util.logging import redact_string

    line = "spark.sql.catalog.fluid.credential=lab-client:Zx9sEcretVaLue123"
    for redact in (redact_secret_text, redact_string):
        masked = redact(line)
        assert "Zx9sEcretVaLue123" not in masked and masked.startswith(
            "spark.sql.catalog.fluid.credential="
        ), masked


def stream_contract():
    """The compiler tests' stream, on this machine (a Lakekeeper table in bucket lake)."""
    from tests.build_runners.spark.test_local import local_contract

    return local_contract()


def test_fluid_apply_starts_leaves_alone_and_stops_a_local_stream(tmp_path, capsys):
    from fluid_build.cli import main

    data = stream_contract()
    path = write(tmp_path, data)
    try:
        assert main(["apply", str(path), "--yes"]) == 0
        assert "Spark ingest-orders (local): started run" in capsys.readouterr().out
        assert main(["apply", str(path), "--yes"]) == 0
        assert "Spark ingest-orders (local): unchanged run" in capsys.readouterr().out
    finally:
        data["builds"] = []
        write(tmp_path, data)
        assert main(["apply", str(path), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Spark ingest-orders (local): stopped run" in out and "CANCELLED" in out


def test_a_declined_apply_starts_no_local_run(tmp_path, monkeypatch):
    """No at apply's prompt applies nothing, and starts no local Spark run either."""
    from fluid_build.cli import apply as apply_cli
    from fluid_build.cli import main

    monkeypatch.setattr(apply_cli, "confirm_action", lambda *a, **k: False)
    monkeypatch.setattr(apply_cli.os, "isatty", lambda fd: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    data = stream_contract()
    path = write(tmp_path, data)
    code = main(["apply", str(path)])
    assert code == 0
    assert not (tmp_path / ".fluid" / "spark-artifacts").exists()
    assert not (tmp_path / ".fluid" / data["id"] / "runtime" / "spark").exists()


def test_an_aws_apply_leaves_this_machine_s_local_runs_alone(tmp_path, monkeypatch):
    """Each environment keeps its own runs: the OpenTofu engine's apply of AWS builds
    reconciles them, and stops none of this machine's local runs."""
    from fluid_build.cli import _apply_opentofu_engine as engine
    from fluid_build.cli import main
    from tests.spark.conftest import CONTRACT

    seen = {}

    def reconcile(runs, logger, **kwargs):
        seen.update(kwargs, builds=[r.build for r in runs])
        return 0

    monkeypatch.setattr(engine, "apply_via_opentofu", lambda *a, **k: 0)
    monkeypatch.setattr(_apply, "reconcile_after_apply", reconcile)
    path = write(tmp_path, CONTRACT)
    assert main(["apply", str(path), "--yes", "--dry-run"]) == 0
    assert seen["builds"] == ["ingest-orders"]
    assert seen["contract"] is None and seen["workspace"] == tmp_path.resolve()


def test_a_contract_that_runs_builds_here_and_on_aws_is_refused(tmp_path, capsys):
    """Outputs on AWS and on this machine make the native engine the apply's, which runs
    no AWS Spark build: the contract is refused, and the hint says to split it."""
    from fluid_build.cli import main
    from tests.spark.conftest import CONTRACT

    local = stream_contract()
    data = {
        **CONTRACT,
        "builds": [
            *CONTRACT["builds"],
            dict(local["builds"][0], id="ingest-here", outputs=["order_lines_here"]),
        ],
        "exposes": [
            *CONTRACT["exposes"],
            dict(local["exposes"][0], exposeId="order_lines_here"),
        ],
    }
    path = write(tmp_path, data)
    code = main(["apply", str(path), "--yes", "--dry-run"])
    assert code == 1
    out = capsys.readouterr().out
    assert "apply_spark_needs_opentofu" in out and "ingest-orders" in out
    assert "ingest-here" not in out.split("error:")[1].split("hint:")[0]
