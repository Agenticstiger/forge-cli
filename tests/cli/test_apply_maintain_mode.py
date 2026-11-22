# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply --mode maintain`` through the CLI entry point: it runs an Iceberg table's
maintenance (``exposes[].iceberg.maintenance``, backend spark) once, on its writer build's
runtime, with no ``tofu``, no plan and no build. ``runtime: local`` is driven by a fake
spark-submit (the Spark local-runtime tests' own stub, which ends a maintenance run at once)."""

from __future__ import annotations

import copy
import json
import logging
import sys
import threading

import pytest
import yaml

from fluid_build.build_runners.spark._local import SUBMIT_EXECUTABLE_ENV, LocalHost, LocalRuns
from fluid_build.cli._common import CLIError
from fluid_build.forge.core.apply_modes import ApplyMode, is_maintain, parse_mode
from tests.build_runners.spark.test_local import STUB, local_contract, wait_for

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="local runs are POSIX only")

LOG = logging.getLogger("test")
PRODUCT = "commerce.orders-stream"
EXPOSE = "order_lines"
BUILD = "ingest-orders"
ROLE = "arn:aws:iam::123456789012:role/glue-optimizer"

#: The runtime-local stub, taught that a maintenance config is a run that ends: it exits with
#: STUB_EXIT, after STUB_MAINTAIN_SLEEP seconds when that is set.
MAINTAIN_STUB = STUB.replace(
    'if mode == "batch":',
    'if mode == "maintenance" and os.environ.get("STUB_MAINTAIN_SLEEP"):\n'
    '    time.sleep(float(os.environ["STUB_MAINTAIN_SLEEP"]))\n'
    'if mode in ("batch", "maintenance"):',
)


@pytest.fixture(autouse=True)
def operator(monkeypatch, tmp_path_factory):
    from fluid_build.spark.compiler import (
        CONTRACT_SQL_ALLOWLIST_ENV,
        LOCAL_NAMESPACE_ALLOWLIST_ENV,
    )

    # Outside the project: forge refuses a spark-submit inside it.
    stub = tmp_path_factory.mktemp("bin") / "spark-submit"
    stub.write_text(MAINTAIN_STUB, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, PRODUCT)
    monkeypatch.setenv(LOCAL_NAMESPACE_ALLOWLIST_ENV, "fluid/commerce")
    monkeypatch.setenv(SUBMIT_EXECUTABLE_ENV, str(stub))
    for name in ("FLUID_SPARK_LOCAL_ROOT", "FLUID_SPARK_SUBMIT_PROPERTIES_FILE", "STUB_EXIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("STUB_MAINTAIN_SLEEP", raising=False)
    return stub


def spark_contract(block=None):
    """The stream of ``local_contract`` on this machine, its table maintained by Spark."""
    data = local_contract()
    data["exposes"][0]["iceberg"] = {
        "maintenance": block or {"schedule": "0 3 * * *", "compaction": {}, "rewriteManifests": {}}
    }
    return data


def glue_contract():
    """A table of the Glue Data Catalog, maintained by Glue's optimizers (no Spark build)."""
    from tests.iac.test_iac_aws_iceberg_metadata import CONTRACT

    data = copy.deepcopy(CONTRACT)
    data["exposes"][0]["iceberg"] = {
        "maintenance": {"backend": "glue", "roleArn": ROLE, "compaction": {}}
    }
    return data


def write(tmp_path, data, name="contract.fluid.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def maintain(path, *extra):
    from fluid_build.cli import main

    return main(["apply", str(path), "--mode", "maintain", "--yes", *extra])


def said(capsys):
    """What the command printed as one line: the console wraps long lines."""
    out = capsys.readouterr()
    return " ".join((out.out + out.err).split())


def parsed(argv):
    import argparse

    from fluid_build.cli.apply import register

    parser = argparse.ArgumentParser(prog="fluid")
    register(parser.add_subparsers(dest="cmd"))
    return parser.parse_args(argv)


def runs_dir(tmp_path):
    return tmp_path / ".fluid" / PRODUCT / "runtime" / "spark" / BUILD / "maintenance" / EXPOSE


def local_runs(tmp_path):
    return LocalRuns(LocalHost(tmp_path), PRODUCT, f"{BUILD}/maintenance/{EXPOSE}")


# --- the mode and its flag --------------------------------------------------------------


def test_maintain_is_an_apply_mode_that_deploys_nothing():
    assert parse_mode("maintain") is ApplyMode.MAINTAIN
    assert is_maintain(ApplyMode.MAINTAIN) and not is_maintain(ApplyMode.AMEND)


def test_the_mode_and_expose_flag_parse():
    args = parsed(["apply", "contract.fluid.yaml", "--mode", "maintain", "--expose-id", "t"])
    assert (args.mode, args.expose_id) == ("maintain", "t")


# --- refusals ---------------------------------------------------------------------------


def test_maintain_refuses_a_plan(tmp_path):
    from fluid_build.cli.apply import run

    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    with pytest.raises(CLIError) as raised:
        run(parsed(["apply", str(plan), "--mode", "maintain", "--yes"]), LOG)
    assert raised.value.event == "apply_maintain_needs_contract"


@pytest.mark.parametrize("mode", ["amend", "dry-run", "reconcile", "amend-and-build"])
def test_expose_id_with_another_mode_is_refused(tmp_path, mode):
    from fluid_build.cli.apply import run

    path = write(tmp_path, spark_contract())
    args = parsed(["apply", str(path), "--mode", mode, "--expose-id", EXPOSE, "--yes"])
    with pytest.raises(CLIError) as raised:
        run(args, LOG)
    assert raised.value.event == "apply_expose_id_requires_maintain_mode"
    assert raised.value.context["expose_id"] == EXPOSE
    assert not (tmp_path / ".fluid").exists()


def test_expose_id_without_a_mode_is_refused(tmp_path):
    from fluid_build.cli.apply import run

    path = write(tmp_path, spark_contract())
    with pytest.raises(CLIError) as raised:
        run(parsed(["apply", str(path), "--expose-id", EXPOSE, "--yes"]), LOG)
    assert raised.value.event == "apply_expose_id_requires_maintain_mode"


def test_an_expose_that_declares_no_maintenance_is_refused(tmp_path):
    from fluid_build.cli.apply import run

    path = write(tmp_path, spark_contract())
    args = parsed(["apply", str(path), "--mode", "maintain", "--expose-id", "nothing", "--yes"])
    with pytest.raises(CLIError) as raised:
        run(args, LOG)
    assert raised.value.event == "apply_maintain_not_declared"
    assert "nothing" in raised.value.context["error"]
    assert not (tmp_path / ".fluid").exists()


def test_a_contract_without_maintenance_has_nothing_to_run(tmp_path, capsys):
    data = local_contract()
    path = write(tmp_path, data)
    assert maintain(path) == 0
    assert "nothing to run" in said(capsys)
    assert not (tmp_path / ".fluid").exists()


def test_an_invalid_maintenance_block_starts_nothing(tmp_path):
    from fluid_build.cli.apply import run

    data = spark_contract({"schedule": "not a cron", "compaction": {}})
    path = write(tmp_path, data)
    with pytest.raises(CLIError) as raised:
        run(parsed(["apply", str(path), "--mode", "maintain", "--yes"]), LOG)
    assert raised.value.event == "apply_maintain_invalid"
    assert "schedule" in raised.value.context["error"]
    assert "nothing was started" in raised.value.context["error"]
    assert not (tmp_path / ".fluid" / PRODUCT).exists()


# --- backend glue: nothing to start -----------------------------------------------------


def test_a_glue_table_prints_the_nothing_to_start_line(tmp_path, capsys):
    path = write(tmp_path, glue_contract())
    assert maintain(path) == 0
    out = said(capsys)
    assert "Iceberg events" in out and "table optimizers maintain it" in out
    assert "nothing to start" in out
    assert not (tmp_path / ".fluid").exists()


def test_a_glue_table_by_expose_id_is_the_same(tmp_path, capsys):
    path = write(tmp_path, glue_contract())
    assert maintain(path, "--expose-id", "events") == 0
    assert "nothing to start" in said(capsys)


# --- backend spark on this machine ------------------------------------------------------


def test_dry_run_says_what_it_would_start_and_starts_nothing(tmp_path, capsys):
    path = write(tmp_path, spark_contract())
    assert maintain(path, "--dry-run") == 0
    out = said(capsys)
    assert f"Iceberg {EXPOSE} maintenance (build {BUILD}, local): would start one run" in out
    assert not (tmp_path / ".fluid").exists()


def test_a_run_starts_through_the_stub_on_the_deployed_config(tmp_path, capsys, monkeypatch):
    argv = tmp_path / "argv"
    argv.mkdir()
    monkeypatch.setenv("STUB_OUT", str(argv))
    path = write(tmp_path, spark_contract())
    assert maintain(path) == 0, said(capsys)
    out = said(capsys)
    assert f"Iceberg {EXPOSE} maintenance (build {BUILD}, local)" in out
    assert "succeeded" in out
    (call,) = [json.loads(p.read_text()) for p in argv.iterdir()]
    config = call["argv"][call["argv"].index("--config") + 1]
    assert config.endswith(f"/{BUILD}/maintenance/{EXPOSE}/job.json")
    assert "--fluid-job-sha256" in call["argv"]
    # its history is its own, beside (not inside) the build's
    (run,) = local_runs(tmp_path).runs()
    assert run.state == "SUCCEEDED"
    assert (runs_dir(tmp_path) / "runs" / run.run_id).is_dir()
    assert not (tmp_path / ".fluid" / PRODUCT / "runtime" / "spark" / BUILD / "runs").exists()


def test_the_exit_code_follows_the_run(tmp_path, capsys, monkeypatch):
    path = write(tmp_path, spark_contract())
    monkeypatch.setenv("STUB_EXIT", "3")
    assert maintain(path) == 1
    out = said(capsys)
    assert "succeeded" not in out
    (run,) = local_runs(tmp_path).runs()
    assert run.state == "FAILED"


def test_expose_id_picks_the_table(tmp_path, capsys):
    path = write(tmp_path, spark_contract())
    assert maintain(path, "--expose-id", EXPOSE) == 0, said(capsys)
    assert len(local_runs(tmp_path).runs()) == 1


def test_it_runs_no_tofu_and_no_provider_apply(tmp_path, monkeypatch, capsys):
    from fluid_build.cli import _apply_opentofu_engine, apply

    def forbidden(*args, **kwargs):
        raise AssertionError("maintain provisioned something")

    monkeypatch.setattr(_apply_opentofu_engine, "apply_via_opentofu", forbidden)
    path = write(tmp_path, spark_contract())
    assert maintain(path) == 0, said(capsys)


def test_the_apply_hooks_and_schema_gate_run_before_a_run_starts(tmp_path, capsys, monkeypatch):
    from fluid_build.cli import apply

    order = []
    real_gate = apply._gate_contract_for_apply
    monkeypatch.setattr(
        apply, "_gate_contract_for_apply", lambda c, log: (order.append("gate"), real_gate(c, log))
    )
    real_hooks = apply._run_apply_hooks_for_source

    def hooks(args, contract, logger):
        order.append("hooks")
        return real_hooks(args, contract, logger)

    monkeypatch.setattr(apply, "_run_apply_hooks_for_source", hooks)
    path = write(tmp_path, spark_contract())
    assert maintain(path) == 0, said(capsys)
    assert order == ["gate", "hooks"]


def test_a_failing_hook_stops_the_run(tmp_path, monkeypatch):
    from fluid_build.cli import apply

    monkeypatch.setattr(apply, "_run_apply_hooks_for_source", lambda a, c, l: 7)
    path = write(tmp_path, spark_contract())
    assert maintain(path) == 7
    assert not (tmp_path / ".fluid" / PRODUCT).exists()


def test_a_retried_call_waits_for_the_run_that_is_going(tmp_path, capsys, monkeypatch):
    """A task retried while its run of the same config is going (its worker was killed before
    it could cancel) waits for that run: a second one would put a second writer on the table."""
    monkeypatch.setenv("STUB_MAINTAIN_SLEEP", "4")
    path = write(tmp_path, spark_contract())
    codes = []
    first = threading.Thread(target=lambda: codes.append(maintain(path)))
    first.start()
    runs = local_runs(tmp_path)
    try:
        assert wait_for(runs.runs, lambda found: any(r.active for r in found))
        capsys.readouterr()
        assert maintain(path) == 0
        assert "already going" in said(capsys)
    finally:
        first.join(timeout=30)
    assert codes == [0]
    assert len(runs.runs()) == 1  # no second run was started
    assert runs.runs()[0].state == "SUCCEEDED"
