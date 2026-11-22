# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Iceberg table maintenance in the plan: ``spark.maintain`` actions are listed after the
build's ``spark.reconcile`` one and bound by ``fluid apply``, and an EMR on EC2 cluster that
runs one step at a time refuses a maintenance step it would hold PENDING."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt

import pytest

from fluid_build.build_runners.spark import _adapters as ad
from fluid_build.build_runners.spark._maintenance import maintenance_runs
from fluid_build.cli import _spark_apply as _apply
from fluid_build.cli._common import CLIError
from tests.build_runners.spark.conftest import ACCOUNT, runs_of, variant

NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)
MAINTENANCE = {
    "schedule": "0 3 * * *",
    "compaction": {"targetFileSizeMb": 256},
    "rewriteManifests": {},
    "expireSnapshots": {"olderThan": "P5D"},
    "removeOrphanFiles": {"olderThan": "P3D"},
}


@pytest.fixture
def maintained(contract):
    contract["exposes"][0]["iceberg"] = {"maintenance": copy.deepcopy(MAINTENANCE)}
    return contract


def _bound_plan(contract, **kwargs):
    return {
        "actions": [
            {"step": 4 + i, "depends_on": ["x"], **a}
            for i, a in enumerate(_apply.planned_actions(contract))
        ],
        **kwargs,
    }


# --- planned_actions --------------------------------------------------------------------


def test_the_plan_lists_the_build_then_the_table_maintenance(maintained):
    build, maintain = _apply.planned_actions(maintained)
    assert (build["op"], build["action_id"]) == ("spark.reconcile", "spark_ingest-orders")
    assert (maintain["op"], maintain["action_type"]) == ("spark.maintain", "spark.maintain")
    assert maintain["action_id"] == "spark_maintain_order_lines"
    assert maintain["id"] == "order_lines"
    for action in (build, maintain):
        assert "step" not in action and "depends_on" not in action
    payload = maintain["payload"]
    assert (payload["expose"], payload["build"]) == ("order_lines", "ingest-orders")
    assert payload["artifacts"]["jobConfig"].endswith(
        "/ingest-orders/maintenance/order_lines/job.json"
    )
    assert payload["configHash"] != build["payload"]["configHash"]
    assert "--mode maintain --expose-id order_lines" in payload["stateImpact"]


def test_a_contract_without_maintenance_plans_the_build_alone(contract):
    (build,) = _apply.planned_actions(contract)
    assert build["op"] == "spark.reconcile"


def test_a_glue_table_adds_no_spark_action(contract):
    contract["exposes"][0]["iceberg"] = {
        "maintenance": {
            "backend": "glue",
            "roleArn": "arn:aws:iam::123456789012:role/glue-optimizer",
            "compaction": {},
        }
    }
    assert [a["op"] for a in _apply.planned_actions(contract)] == ["spark.reconcile"]


# --- prepare ----------------------------------------------------------------------------


def test_prepare_takes_a_plan_with_both_actions(maintained):
    runs = _apply.prepare(
        maintained, build_id=None, verified_plan=_bound_plan(maintained), dry_run=True
    )
    # The builds' runs only: the maintenance runs are compiled for the plan check and
    # deployed by OpenTofu, never reconciled as a stream.
    assert [r.build for r in runs] == ["ingest-orders"]
    assert all(r.maintains is None for r in runs)


def test_prepare_takes_an_unbound_apply_of_a_maintained_contract(maintained):
    assert _apply.prepare(maintained, build_id=None, verified_plan=None, dry_run=True)


def _refused(contract, plan):
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    assert err.value.event == "apply_spark_artifact_mismatch"
    assert "spark.maintain" in err.value.context["error"]


def test_prepare_refuses_another_maintenance_config_hash(maintained):
    plan = _bound_plan(maintained)
    plan["actions"][1]["payload"]["configHash"] = "0" * 64
    _refused(maintained, plan)


def test_prepare_refuses_a_plan_made_before_the_maintenance_changed(maintained):
    plan = _bound_plan(maintained)
    maintained["exposes"][0]["iceberg"]["maintenance"]["expireSnapshots"] = {"olderThan": "P9D"}
    _refused(maintained, plan)


def test_prepare_refuses_a_plan_without_the_maintenance_action(maintained):
    plan = _bound_plan(maintained)
    plan["actions"] = [a for a in plan["actions"] if a["op"] != "spark.maintain"]
    _refused(maintained, plan)


def test_prepare_refuses_a_maintenance_action_the_contract_does_not_declare(maintained):
    plan = _bound_plan(maintained)
    del maintained["exposes"][0]["iceberg"]
    _refused(maintained, plan)


def test_prepare_refuses_a_second_maintenance_action(maintained):
    plan = _bound_plan(maintained)
    extra = copy.deepcopy(plan["actions"][1])
    extra["step"] = 9
    plan["actions"].append(extra)
    _refused(maintained, plan)


def test_prepare_refuses_a_maintain_action_for_a_contract_with_no_spark_build(maintained):
    plan = _bound_plan(maintained)
    maintained["builds"] = []
    with pytest.raises(CLIError) as err:
        _apply.prepare(maintained, build_id=None, verified_plan=plan, dry_run=True)
    assert err.value.event == "apply_spark_artifact_mismatch"


def test_a_plan_with_only_a_maintain_action_is_refused_without_a_build(contract):
    contract["builds"] = []
    plan = {"actions": [{"step": 1, "depends_on": [], "op": "spark.maintain", "payload": {}}]}
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    assert err.value.event == "apply_spark_artifact_mismatch"


# --- the maintenance run itself ---------------------------------------------------------


def test_a_maintenance_run_is_its_own_run_of_the_builds_runtime(maintained):
    (build,) = runs_of(maintained)
    (run,) = maintenance_runs(maintained, account=ACCOUNT)
    assert run.maintains == "order_lines" and run.scheduled
    assert run.run_key == "ingest-orders/maintenance/order_lines"
    assert build.run_key == "ingest-orders"
    assert (run.runtime, run.target, run.role_arn) == (build.runtime, build.target, build.role_arn)
    assert run.run_name != build.run_name and run.config_hash != build.config_hash
    assert run.retry_attempts is None and run.restarts_per_hour == 0
    assert run.tags()["fluid_maintains"] == "order_lines"
    assert "fluid_maintains" not in build.tags()


# --- EMR on EC2: one step at a time -----------------------------------------------------

boto3 = pytest.importorskip("boto3")
from tests.build_runners.spark.test_adapters import (  # noqa: E402
    _step,
    adapter_on,
    started,
)


def _ec2_maintenance(maintained):
    (run,) = maintenance_runs(maintained, account=ACCOUNT)
    return variant(run, "emr-ec2", {"clusterId": "j-ABC"})


def _cluster(level):
    cluster = {"Id": "j-ABC", "Status": {"State": "WAITING", "StateChangeReason": {}}}
    if level is not None:
        cluster["StepConcurrencyLevel"] = level
    return {"Cluster": cluster}


def _add_step(stub, run):
    args = ["spark-submit", "--deploy-mode", "client", *ad._conf_params(run.spark_conf)]
    args += [run.runner_uri, *started(run).entry_args(stop_key="tok")]
    stub.add_response(
        "add_job_flow_steps",
        {"StepIds": ["s-9"]},
        {"JobFlowId": "j-ABC", "Steps": [{
            "Name": run.run_name,
            "ActionOnFailure": "CONTINUE",
            "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args},
        }]},
    )  # fmt: skip


def _running(stub, run, *steps):
    stub.add_response(
        "list_steps",
        {"Steps": list(steps)},
        {"ClusterId": "j-ABC", "StepStates": ["RUNNING"]},
    )


def test_a_maintenance_step_is_refused_behind_a_running_step_at_concurrency_1(maintained):
    run = _ec2_maintenance(maintained)
    adapter, stub = adapter_on(run, "emr")
    stub.add_response("describe_cluster", _cluster(1), {"ClusterId": "j-ABC"})
    _running(stub, run, _step(run, "s-stream", "RUNNING", NOW, name="stream"))
    with pytest.raises(ad.SparkRunError) as err:
        adapter.start("tok")
    message = str(err.value)
    assert "s-stream" in message and "StepConcurrencyLevel 1" in message
    assert "aws emr modify-cluster --cluster-id j-ABC --step-concurrency-level 2" in message
    stub.assert_no_pending_responses()  # no step was added


def test_a_cluster_that_runs_steps_side_by_side_takes_the_maintenance_step(maintained):
    run = _ec2_maintenance(maintained)
    adapter, stub = adapter_on(run, "emr")
    stub.add_response("describe_cluster", _cluster(2), {"ClusterId": "j-ABC"})
    _add_step(stub, run)
    assert adapter.start("tok") == "s-9"
    stub.assert_no_pending_responses()


def test_an_idle_cluster_at_concurrency_1_takes_the_maintenance_step(maintained):
    run = _ec2_maintenance(maintained)
    adapter, stub = adapter_on(run, "emr")
    stub.add_response("describe_cluster", _cluster(1), {"ClusterId": "j-ABC"})
    _running(stub, run)
    _add_step(stub, run)
    assert adapter.start("tok") == "s-9"


def test_its_own_running_step_does_not_count_as_busy(maintained):
    run = _ec2_maintenance(maintained)
    adapter, stub = adapter_on(run, "emr")
    stub.add_response("describe_cluster", _cluster(1), {"ClusterId": "j-ABC"})
    _running(stub, run, _step(run, "s-self", "RUNNING", NOW))
    _add_step(stub, run)
    assert adapter.start("tok") == "s-9"


def test_a_cluster_that_reports_no_level_is_left_to_emr(maintained):
    run = _ec2_maintenance(maintained)
    adapter, stub = adapter_on(run, "emr")
    stub.add_response("describe_cluster", _cluster(None), {"ClusterId": "j-ABC"})
    _add_step(stub, run)
    assert adapter.start("tok") == "s-9"


def test_an_unreadable_cluster_is_left_to_emr(maintained):
    run = _ec2_maintenance(maintained)
    adapter, stub = adapter_on(run, "emr")
    stub.add_client_error("describe_cluster", "AccessDeniedException", "not allowed")
    _add_step(stub, run)
    assert adapter.start("tok") == "s-9"


def test_a_builds_own_run_is_not_checked_for_concurrency(run):
    ec2 = variant(run, "emr-ec2", {"clusterId": "j-ABC"})
    assert ec2.maintains is None
    adapter, stub = adapter_on(ec2, "emr")
    _add_step(stub, ec2)  # no describe_cluster, no list_steps
    assert adapter.start("tok") == "s-9"
    stub.assert_no_pending_responses()


def test_the_local_adapter_keeps_a_maintenance_runs_history_apart(maintained, tmp_path):
    import sys

    if sys.platform == "win32":
        pytest.skip("local runs are POSIX only")
    from fluid_build.build_runners.spark._local import LocalHost

    (run,) = maintenance_runs(maintained, account=ACCOUNT)
    local = dataclasses.replace(run, runtime="local", target={})
    adapter = ad.Local(local, LocalHost(tmp_path))
    assert adapter._runs.dir.parts[-3:] == ("ingest-orders", "maintenance", "order_lines")
    plain = ad.Local(dataclasses.replace(run, runtime="local", target={}, maintains=None),
                     LocalHost(tmp_path))  # fmt: skip
    assert plain._runs.dir.parts[-1] == "ingest-orders"
