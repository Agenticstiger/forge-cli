# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What ``fluid apply`` hands the runner to stamp on each Iceberg commit.

The product version, the contract's digest, the release and the pipeline's run id reach the
runner as arguments. They are never in the job config, the Spark configuration or the plan:
a contract edit that does not change the job, or a new run id, must not change the config hash,
or a running stream would be replaced by every deploy.
"""

from __future__ import annotations

import logging
import re

import pytest

from fluid_build.build_runners.spark import _desired
from fluid_build.build_runners.spark._adapters import _identity
from fluid_build.cli import _spark_apply as _apply
from fluid_build.cli._common import CLIError
from fluid_build.forge.core.plan_digest import compute_contract_digest
from fluid_build.spark import runner_source
from tests.build_runners.spark.conftest import runs_of

DIGEST = "sha256:" + "ab" * 32


@pytest.fixture(autouse=True)
def _account(monkeypatch):
    monkeypatch.setattr(
        "fluid_build.providers.aws.util.config.resolve_account_and_region",
        lambda *a, **k: ("123456789012", "eu-west-1"),
    )
    monkeypatch.delenv(_desired.RELEASE_ID_ENV, raising=False)


def stamps(**given):
    return {"contract_digest": DIGEST, "run_id": "3f9a3f9a3f9a", "release_id": "v1.4.0", **given}


def flag_values(run):
    args = run.stamp_args()
    return dict(zip(args[::2], args[1::2], strict=True))


# --- where the stamps are -------------------------------------------------------------


def test_a_run_started_with_stamps_passes_them_to_the_runner(contract):
    contract["exposes"][0]["version"] = "1.4.0"
    run = runs_of(contract, stamps=stamps())[0]
    assert flag_values(run) == {
        "--fluid-product-version": "1.4.0",
        "--fluid-contract-digest": DIGEST,
        "--fluid-release-id": "v1.4.0",
        "--fluid-run-id": "3f9a3f9a3f9a",
    }
    args = run.entry_args(restart_of="run-1")
    assert args.index("--fluid-run-id") < args.index(_desired.RESTART_FLAG)
    assert set(flag_values(run).items()) <= set(zip(args, args[1:], strict=False))


def test_a_run_without_stamps_passes_none(run):
    assert run.stamp_args() == [] and "--fluid-run-id" not in run.entry_args()


def test_the_stamps_never_change_the_config_hash_the_plan_or_the_spark_conf(contract):
    contract["exposes"][0]["version"] = "1.4.0"
    plain = runs_of(contract)[0]
    stamped = runs_of(contract, stamps=stamps())[0]
    assert stamped.config_hash == plain.config_hash
    assert stamped.plan_action(0) == plain.plan_action(0)
    assert stamped.spark_conf == plain.spark_conf and stamped.job_text == plain.job_text
    assert stamped.tags() == plain.tags()
    assert stamped == plain  # the stamps are not part of what two runs are compared by


def test_a_new_run_id_or_release_restarts_nothing(contract):
    first = runs_of(contract, stamps=stamps(run_id="aaaa"))[0]
    second = runs_of(contract, stamps=stamps(run_id="bbbb", release_id="v1.5.0"))[0]
    assert first.config_hash == second.config_hash and first.entry_args() != second.entry_args()


def test_a_contract_edit_that_leaves_the_job_alone_changes_the_digest_not_the_hash(contract):
    before = _desired.run_stamps(contract)["contract_digest"]
    hash_before = runs_of(contract)[0].config_hash
    contract["description"] = "the orders, with a new description"
    assert _desired.run_stamps(contract)["contract_digest"] != before
    assert runs_of(contract)[0].config_hash == hash_before


def test_the_runner_reads_the_flags_these_runs_pass():
    source = runner_source()
    for flag in ("--fluid-product-version", "--fluid-contract-digest"):
        assert f'"{flag}"' in source
    for flag in ("--fluid-release-id", "--fluid-run-id"):
        assert f'"{flag}"' in source


def test_forge_and_the_runner_accept_the_same_values():
    found = re.search(r'_STAMP_VALUE = re\.compile\(r"(.+)"\)', runner_source())
    assert found and found.group(1) == _desired.STAMP_VALUE.pattern


def test_adapters_read_back_the_run_identity_from_the_flags_and_not_the_stamps(contract):
    run = runs_of(contract, stamps=stamps())[0]
    assert set(_identity(run.entry_args())) <= {"config_hash", "source_id", "checkpoint_id"}


# --- the version ----------------------------------------------------------------------


def test_the_version_is_the_one_of_the_builds_output(contract):
    assert runs_of(contract)[0].product_version is None  # the example declares none
    contract["exposes"][0]["version"] = "2.0.1-rc.1+build.5"
    assert runs_of(contract)[0].product_version == "2.0.1-rc.1+build.5"


@pytest.mark.parametrize("version", ["two words", "1.0\n1.1", "", 3, None, "x" * 300])
def test_a_version_a_stamp_may_not_hold_is_left_unstamped(contract, version):
    contract["exposes"][0]["version"] = version
    assert runs_of(contract)[0].product_version is None


def test_the_white_space_around_a_version_is_not_part_of_it(contract):
    contract["exposes"][0]["version"] = " 1.0.0\n"
    assert runs_of(contract)[0].product_version == "1.0.0"


# --- what apply hands over ------------------------------------------------------------


def test_the_digest_is_the_contracts_and_holds_no_environment_value(contract, monkeypatch):
    contract["exposes"][0]["description"] = "{{ env.REGION_NAME }}"
    monkeypatch.setenv("REGION_NAME", "eu-west-1")
    got = _desired.run_stamps(contract)["contract_digest"]
    assert got == compute_contract_digest(contract)  # as loaded, templates unresolved
    assert "eu-west-1" not in got


def test_the_run_id_is_kept_as_it_is_when_it_can_be_stamped(contract):
    for run_id in ("3f9a3f9a3f9a", "scheduled__2026-11-21T00:00:00+00:00"):
        assert _desired.run_stamps(contract, run_id=run_id)["run_id"] == run_id


def test_an_operators_run_id_that_a_stamp_may_not_hold_is_made_safe_not_refused(contract):
    got = _desired.run_stamps(contract, run_id="build 42;\nnext")["run_id"]
    assert got == "build_42__next" and _desired.STAMP_VALUE.fullmatch(got)
    assert len(_desired.run_stamps(contract, run_id="r" * 400)["run_id"]) == 256
    assert "run_id" not in _desired.run_stamps(contract)
    # never an option of the command line: what is not a letter or a digit is cut from its start
    assert _desired.run_stamps(contract, run_id="--config=s3://x/job.json")["run_id"] == (
        "config=s3://x/job.json"
    )
    assert "run_id" not in _desired.run_stamps(contract, run_id="--- ")


def test_the_release_is_the_one_the_deploy_names(contract):
    assert "release_id" not in _desired.run_stamps(contract, environ={})
    got = _desired.run_stamps(contract, environ={_desired.RELEASE_ID_ENV: " v1.4.0 "})
    assert got["release_id"] == "v1.4.0"


@pytest.mark.parametrize("value", ["two words", "a;b", "x" * 257, "v1\nv2", "-v1", "--config=x"])
def test_a_release_a_stamp_may_not_hold_is_refused_by_name(contract, value):
    with pytest.raises(ValueError, match=_desired.RELEASE_ID_ENV) as refused:
        _desired.run_stamps(contract, environ={_desired.RELEASE_ID_ENV: value})
    assert "nothing was started" in str(refused.value) and "x" * 100 not in str(refused.value)


def test_a_contract_no_digest_can_hold_is_left_unstamped(contract, monkeypatch):
    def refuse(_contract):
        raise TypeError("not JSON")

    monkeypatch.setattr("fluid_build.forge.core.plan_digest.compute_contract_digest", refuse)
    assert _desired.run_stamps(contract, run_id="r-1") == {"run_id": "r-1"}


# --- the apply hooks ------------------------------------------------------------------


def test_prepare_hands_each_run_its_stamps(contract, monkeypatch):
    monkeypatch.setenv(_desired.RELEASE_ID_ENV, "v1.4.0")
    contract["exposes"][0]["version"] = "1.4.0"
    (run,) = _apply.prepare(
        contract, build_id=None, verified_plan=None, dry_run=True, run_id="3f9a3f9a3f9a"
    )
    assert run.run_id == "3f9a3f9a3f9a" and run.release_id == "v1.4.0"
    assert run.contract_digest == compute_contract_digest(contract)
    assert run.product_version == "1.4.0"


def test_prepare_accepts_a_bound_plan_whatever_the_stamps(contract, monkeypatch):
    plan = {
        "actions": _apply.planned_actions(contract),
        "planDigest": "x",
    }
    monkeypatch.setenv(_desired.RELEASE_ID_ENV, "v9")
    (run,) = _apply.prepare(
        contract, build_id=None, verified_plan=plan, dry_run=True, run_id="another-run"
    )
    assert run.release_id == "v9"  # a plan made with other stamps still binds


def test_prepare_refuses_a_release_id_a_stamp_may_not_hold_before_anything_starts(
    contract, monkeypatch
):
    monkeypatch.setenv(_desired.RELEASE_ID_ENV, "two words")
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=None, dry_run=True)
    assert err.value.event == "apply_spark_invalid"
    assert _desired.RELEASE_ID_ENV in str(err.value.context["error"])


def test_a_contract_without_a_spark_build_never_reads_the_release_variable(contract, monkeypatch):
    monkeypatch.setenv(_desired.RELEASE_ID_ENV, "two words")
    contract["builds"] = []
    assert _apply.prepare(contract, build_id=None, verified_plan=None, dry_run=True) == []


def test_reconcile_only_stamps_the_runs_it_starts(contract, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "fluid_build.build_runners.spark._reconcile.reconcile",
        lambda runs, *a, **k: seen.extend(runs) or [],
    )
    monkeypatch.setattr(_apply, "_require_boto3", lambda: None)
    try:
        _apply.reconcile_only(
            contract,
            logging.getLogger("test"),
            build_id=None,
            dry_run=True,
            run_id="3f9a3f9a3f9a",
        )
    except Exception:  # noqa: BLE001 - what it does next is not this test's
        pass
    assert all(run.run_id == "3f9a3f9a3f9a" for run in seen)
