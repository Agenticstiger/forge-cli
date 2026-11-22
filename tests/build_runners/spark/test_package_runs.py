# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A prebuilt package build's run: what fluid plan shows and binds, and where a local run's
copy of the package comes from."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path

import pytest

from fluid_build.build_runners.spark import _local
from fluid_build.build_runners.spark._adapters import SparkRunError
from fluid_build.spark import names as _names
from fluid_build.spark import runner_source
from fluid_build.spark.package import PACKAGE_ALLOWLIST_ENV
from tests.build_runners.spark.conftest import ACCOUNT, runs_of
from tests.spark.package_fixtures import PACKAGE, PACKAGE_CONTRACT, SHA, SOURCE

PRODUCT, BUILD = "commerce.orders-silver", "merge-orders"


@pytest.fixture(autouse=True)
def listed(monkeypatch):
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, "s3://commerce-artifacts/code/")


@pytest.fixture
def contract():
    return copy.deepcopy(PACKAGE_CONTRACT)


def test_the_plan_shows_the_package_it_runs(contract):
    (run,) = runs_of(contract)
    payload = run.plan_action(1)["payload"]
    assert payload["package"] == {
        "source": SOURCE,
        "sha256": SHA,
        "entryPoint": "orders_pkg.jobs:merge_orders",
    }
    assert payload["artifacts"]["package"] == _names.package_uri(
        "s3://commerce-lake", PRODUCT, BUILD, SHA
    )
    assert payload["scheduled"] is True and payload["mode"] == "batch"


def test_the_plan_binds_where_the_package_comes_from(contract):
    """A plan made with one source is not the apply's with another, even for the same
    digest: the plan names where the bytes are copied from."""
    (before,) = runs_of(contract)
    contract["builds"][0]["properties"]["package"]["uri"] = SOURCE.replace("code/", "code/v2/")
    (after,) = runs_of(contract)
    assert before.plan_action(1) != after.plan_action(1)
    assert before.config_hash == after.config_hash  # the same bytes: no new run needed


def test_a_new_digest_is_a_new_run(contract):
    (before,) = runs_of(contract)
    contract["builds"][0]["properties"]["package"]["sha256"] = "1" * 64
    (after,) = runs_of(contract)
    assert after.config_hash != before.config_hash
    assert after.package_uri != before.package_uri


def test_the_operator_s_list_refuses_the_run(contract, monkeypatch):
    from fluid_build.spark.policy import SparkPolicyError

    monkeypatch.delenv(PACKAGE_ALLOWLIST_ENV)
    with pytest.raises(SparkPolicyError):
        runs_of(contract)
    assert runs_of(contract, policy=False)[0].package_sha256 == SHA


def test_a_build_without_a_package_plans_as_before():
    from tests.spark.conftest import CONTRACT

    (run,) = runs_of(copy.deepcopy(CONTRACT))
    payload = run.plan_action(1)["payload"]
    assert "package" not in payload and set(payload["artifacts"]) == {"jobConfig", "runner"}
    assert run.package_uri is None


# --- plan and apply -----------------------------------------------------------------------


@pytest.fixture
def _account(monkeypatch):
    monkeypatch.setattr(
        "fluid_build.providers.aws.util.config.resolve_account_and_region",
        lambda *a, **k: (ACCOUNT, "eu-west-1"),
    )


def _bound_plan(contract):
    from fluid_build.cli import _spark_apply

    return {"actions": [{"step": 2, **a} for a in _spark_apply.planned_actions(contract)]}


def test_the_plan_lists_the_package_before_the_operator_has(contract, monkeypatch, _account):
    """fluid plan does not apply the operator's gates (fluid validate warns): it lists the
    run, and apply refuses it until the store is listed."""
    from fluid_build.cli import _spark_apply
    from fluid_build.cli._common import CLIError

    monkeypatch.delenv(PACKAGE_ALLOWLIST_ENV)
    (action,) = _spark_apply.planned_actions(contract)
    assert action["payload"]["package"]["sha256"] == SHA
    with pytest.raises(CLIError) as caught:
        _spark_apply.prepare(contract, build_id=None, verified_plan=None, dry_run=True)
    assert caught.value.event == "apply_spark_refused"
    assert PACKAGE_ALLOWLIST_ENV in caught.value.context["hint"]


def test_apply_refuses_a_plan_made_for_another_package_source(contract, _account):
    from fluid_build.cli import _spark_apply
    from fluid_build.cli._common import CLIError

    plan = _bound_plan(contract)
    assert _spark_apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    contract["builds"][0]["properties"]["package"]["uri"] = SOURCE.replace("code/", "code/v2/")
    with pytest.raises(CLIError) as caught:
        _spark_apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    assert caught.value.event == "apply_spark_artifact_mismatch"
    assert "package" in caught.value.context["error"]


# --- a local run's copy ------------------------------------------------------------------


def _local_contract(contract, wheel: Path):
    build = contract["builds"][0]
    build["properties"]["parameters"]["spark"] = {"runtime": "local"}
    build["properties"]["package"]["uri"] = f"file://{wheel}"
    contract["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "iceberg",
        "location": {"catalog": "lakekeeper", "database": "commerce", "table": "orders"},
    }
    return contract


@pytest.fixture
def local_run(contract, tmp_path, monkeypatch):
    wheel = tmp_path / "dist" / "orders_pkg-1.4.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(PACKAGE)
    root = tmp_path / "artifacts"
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, f"file://{wheel.parent}/")
    monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    (run,) = runs_of(_local_contract(contract, wheel))
    return run.bind(local_root=str(root)), wheel, root


def _submitter(root: Path, env=None):
    return _local.Submitter(
        executable="spark-submit",
        properties_file=None,
        properties={},
        root=str(root),
        # The operator's environment: the package list the fixture set among it.
        env=env if env is not None else dict(os.environ),
        confine=None,
    )


def test_a_local_run_copies_the_pinned_package_beside_its_config(local_run):
    run, _, root = local_run
    assert run.package_uri == _names.package_uri(str(root), PRODUCT, BUILD, SHA)
    _local.publish(_submitter(root), run, runner_source())
    assert Path(run.package_uri).read_bytes() == PACKAGE
    assert Path(run.job_uri).is_file()


def test_a_local_package_that_changed_is_refused(local_run):
    run, wheel, root = local_run
    wheel.write_bytes(PACKAGE + b"\0")
    with pytest.raises(SparkRunError, match="not the pinned package") as caught:
        _local.publish(_submitter(root), run, runner_source())
    assert hashlib.sha256(PACKAGE + b"\0").hexdigest() in str(caught.value)
    assert not Path(run.package_uri).exists()


def test_a_local_package_outside_the_listed_places_is_refused(local_run, tmp_path, monkeypatch):
    """The real file counts: a link in a listed folder to a file elsewhere is refused."""
    run, wheel, root = local_run
    elsewhere = tmp_path / "elsewhere.whl"
    elsewhere.write_bytes(PACKAGE)
    wheel.unlink()
    wheel.symlink_to(elsewhere)
    with pytest.raises(SparkRunError, match=PACKAGE_ALLOWLIST_ENV):
        _local.publish(_submitter(root), run, runner_source())


def test_a_missing_local_package_says_where_it_looked(local_run):
    run, wheel, root = local_run
    wheel.unlink()
    with pytest.raises(SparkRunError, match="cannot be read"):
        _local.publish(_submitter(root), run, runner_source())


def test_runs_bind_the_package_copy_to_the_account(contract):
    contract["exposes"][0]["binding"]["location"].pop("bucket")
    (unbound,) = runs_of(contract, account=None)
    assert "${aws_account_id}" in unbound.package_uri
    assert ACCOUNT in unbound.bind(ACCOUNT).package_uri
