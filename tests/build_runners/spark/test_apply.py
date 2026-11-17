# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The plan and apply hooks: planned_actions, prepare, reconcile_after_apply."""

from __future__ import annotations

import logging

import pytest

from fluid_build.build_runners.spark._reconcile import Outcome
from fluid_build.cli import _spark_apply as _apply
from fluid_build.cli._common import CLIError
from tests.build_runners.spark.conftest import ACCOUNT, runs_of

LOG = logging.getLogger("test")


@pytest.fixture(autouse=True)
def _account(monkeypatch):
    """The account the default role needs, without STS."""
    monkeypatch.setattr(
        "fluid_build.providers.aws.util.config.resolve_account_and_region",
        lambda *a, **k: ("123456789012", "eu-west-1"),
    )


def test_a_contract_without_a_spark_build_plans_nothing(contract):
    contract["builds"] = []
    assert _apply.planned_actions(contract) == []


def test_a_pre_0_7_7_contract_plans_nothing(contract):
    contract["fluidVersion"] = "0.7.6"
    assert _apply.planned_actions(contract) == []


def test_planned_actions_are_the_runs_actions_without_step_or_dependencies(contract):
    (action,) = _apply.planned_actions(contract)
    assert action["op"] == "spark.reconcile"
    assert "step" not in action and "depends_on" not in action


def _bound_plan(contract):
    return {
        "actions": [{"step": 4, "depends_on": ["x"], **a} for a in _apply.planned_actions(contract)]
    }


def test_prepare_accepts_a_plan_that_matches_whatever_the_step(contract):
    runs = _apply.prepare(
        contract, build_id=None, verified_plan=_bound_plan(contract), dry_run=True
    )
    assert [r.build for r in runs] == ["ingest-orders"]


def test_prepare_refuses_a_tampered_config_hash(contract):
    plan = _bound_plan(contract)
    plan["actions"][0]["payload"]["configHash"] = "0" * 64
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    assert err.value.event == "apply_spark_artifact_mismatch"


def test_prepare_refuses_a_plan_made_with_another_maven_mirror(contract, monkeypatch):
    from fluid_build.build_runners.spark import _desired

    monkeypatch.delenv(_desired.MAVEN_REPOSITORY_ENV, raising=False)
    plan = _bound_plan(contract)
    monkeypatch.setenv(_desired.MAVEN_REPOSITORY_ENV, "https://mirror.example.internal/maven/")
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    assert err.value.event == "apply_spark_artifact_mismatch"
    assert _desired.MAVEN_REPOSITORY_ENV in err.value.context["error"]


def test_prepare_refuses_a_plan_that_starts_runs_the_contract_dropped(contract):
    plan = _bound_plan(contract)
    contract["builds"] = []
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    assert err.value.event == "apply_spark_artifact_mismatch"


def test_prepare_needs_boto3_for_a_streaming_build_unless_dry_run(contract, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=None, dry_run=False)
    assert err.value.event == "apply_aws_sdk_missing"
    assert _apply.prepare(contract, build_id=None, verified_plan=None, dry_run=True)


def test_prepare_selects_the_build(contract):
    assert _apply.prepare(contract, build_id="other", verified_plan=None, dry_run=True) == []


def test_a_dry_run_reconcile_touches_no_aws(run, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("AWS was touched")

    monkeypatch.setattr("fluid_build.build_runners.spark._reconcile.adapter_for", forbidden)
    assert _apply.reconcile_after_apply([run], LOG, dry_run=True) == 0
    assert _apply.reconcile_after_apply([], LOG, dry_run=False) == 0


@pytest.mark.parametrize(("error", "code"), [(True, 1), (False, 0)])
def test_reconcile_after_apply_fails_when_a_build_errored(run, monkeypatch, error, code):
    monkeypatch.setattr(
        "fluid_build.build_runners.spark._reconcile.reconcile",
        lambda runs, **kw: [Outcome(run.build, run.runtime, "failed", error=error)],
    )
    assert _apply.reconcile_after_apply([run], LOG, dry_run=False) == code


def test_the_plan_hook_appends_one_spark_action_at_step_one(contract, monkeypatch):
    from types import SimpleNamespace

    from fluid_build.cli import plan as plan_mod

    monkeypatch.setattr(plan_mod, "_output_port_actions", lambda c, a, log: [])
    plan = {"actions": []}
    plan_mod._append_output_port_actions(
        plan, contract, SimpleNamespace(provider=None, contract=None), LOG
    )
    assert [(a["step"], a["op"]) for a in plan["actions"]] == [(1, "spark.reconcile")]
    assert plan["total_actions"] == 1
    assert plan["actions"][0]["payload"]["configHash"] == runs_of(contract)[0].config_hash


def test_a_plan_does_not_apply_the_operators_gates_and_matches_the_gated_apply(
    contract, monkeypatch
):
    from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

    monkeypatch.delenv(HOST_ALLOWLIST_ENV, raising=False)
    planned = _apply.planned_actions(contract)  # fluid validate warns; plan still lists it
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example")
    runs = _apply.prepare(contract, build_id=None, verified_plan={"actions": planned}, dry_run=True)
    assert [r.config_hash for r in runs] == [a["payload"]["configHash"] for a in planned]


def test_an_undeclared_region_is_resolved_when_the_run_starts(contract, monkeypatch):
    pytest.importorskip("boto3")
    from fluid_build.build_runners.spark import _adapters, _desired

    del contract["exposes"][0]["binding"]["location"]["region"]
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    (action,) = _apply.planned_actions(contract)
    assert action["payload"]["region"] == _desired.REGION_TOKEN
    (run,) = _desired.desired_runs(contract, account=ACCOUNT)
    adapter = _adapters.adapter_for(
        run, lambda service, region: __import__("boto3").client(service, region_name=region)
    )
    assert adapter.run.region == "ca-central-1"
    assert adapter.run.role_arn.startswith(f"arn:aws:iam::{ACCOUNT}:role/")


def test_spark_builds_on_the_native_engine_are_refused(contract):
    from fluid_build.cli._common import CLIError

    with pytest.raises(CLIError) as err:
        _apply.refuse_without_opentofu(contract, "native")
    assert err.value.event == "apply_spark_needs_opentofu"
    _apply.refuse_without_opentofu({"fluidVersion": "0.7.7", "builds": []}, "native")
