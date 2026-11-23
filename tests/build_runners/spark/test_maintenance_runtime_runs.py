# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``maintenance_runs`` for a table no forge build writes (``iceberg.maintenance.runtime``).

With ``builds: []`` the run is built from the block alone: the runtime, target and role it
names, a job config compiled from the expose's binding, and artifacts in the table's bucket
under ``maintain_<expose>``. The operator's role and target lists apply to it unless a plan asks
for the run without them. The last test submits one to moto's EMR Serverless.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from typing import Any, Dict, List

import pytest

from fluid_build._errors import FluidUserError
from fluid_build.build_runners.spark._adapters import SparkRunError, adapter_for, checked_run
from fluid_build.build_runners.spark._maintenance import maintenance_runs
from fluid_build.iac.providers import aws_identity
from fluid_build.spark import names, runner_source
from fluid_build.spark.policy import SparkPolicyError
from tests.build_runners.spark.conftest import ACCOUNT
from tests.maintenance.conftest import EXPOSE, maintained, spark_block

pytestmark = pytest.mark.unit

ROLE = "arn:aws:iam::123456789012:role/orders-maint"
APP = "app0000000001"
PRODUCT = "commerce.orders-stream"
BUILD = "maintain_order_lines"
PREFIX = f"s3://commerce-lake/_fluid/spark/{PRODUCT}/{BUILD}"


def runtime(**extra: Any) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "runtime": "emr-serverless",
        "applicationId": APP,
        "executionRoleArn": ROLE,
    }
    block.update(extra)
    return block


def unwritten(**extra: Any) -> Dict[str, Any]:
    """The example contract with no build: nothing forge runs writes ``order_lines``."""
    block = spark_block(
        runtime=extra.pop("rt", None) or runtime(),
        removeOrphanFiles={"olderThan": "P7D"},
        **extra,
    )
    contract = maintained(block)
    contract["builds"] = []
    return contract


@pytest.fixture(autouse=True)
def operator_lists(monkeypatch):
    """The operator lists the example's role and application, and nothing else."""
    monkeypatch.setenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, ROLE)
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, f"emr-serverless:{APP}")
    monkeypatch.delenv(aws_identity.SPARK_CONF_ALLOWLIST_ENV, raising=False)


def one(contract=None, **kwargs):
    runs = maintenance_runs(contract if contract is not None else unwritten(), **kwargs)
    assert len(runs) == 1
    return runs[0]


def test_the_run_is_named_and_placed_from_the_block() -> None:
    run = one(account=ACCOUNT)
    assert run.build == BUILD
    assert run.maintenance_runtime is True and run.maintains == EXPOSE
    assert run.runtime == "emr-serverless"
    assert run.target == {"applicationId": APP}
    assert run.role_arn == ROLE
    assert run.mode == "maintenance"
    assert run.scheduled is True
    assert run.retry_attempts is None and run.restarts_per_hour == 0
    assert run.run_name == names.maintenance_run_name(PRODUCT, EXPOSE)
    assert run.run_name.endswith("-maintain")
    assert run.region == "eu-west-1"


def test_artifacts_sit_in_the_tables_bucket_under_the_maintenance_name() -> None:
    run = one(account=ACCOUNT)
    assert run.job_uri == f"{PREFIX}/maintenance/{EXPOSE}/job.json"
    assert re.fullmatch(
        re.escape(f"{PREFIX}/runner/") + r"[0-9a-f]{16}/forge_spark_runner\.py", run.runner_uri
    )
    assert run.runner_uri == names.runner_uri(
        "s3://commerce-lake", PRODUCT, BUILD, run.runner_sha256
    )


def test_the_tags_say_what_the_run_maintains() -> None:
    tags = one(account=ACCOUNT).tags()
    assert tags["fluid_product"] == PRODUCT
    assert tags["fluid_build"] == BUILD
    assert tags["fluid_maintains"] == EXPOSE


def test_the_job_config_is_a_maintenance_job_on_the_exposes_table() -> None:
    config = json.loads(one(account=ACCOUNT).job_text)
    assert config["mode"] == "maintenance"
    assert config["build"] == BUILD and config["product"] == PRODUCT
    sink = config["sink"]
    assert sink["type"] == "glue"
    assert sink["table"] == "commerce.order_lines"
    assert sink["region"] == "eu-west-1"
    assert sink["warehouse"] == "s3://commerce-lake/iceberg/commerce/order_lines/"
    assert "create" not in sink
    assert config["maintenance"]["expose"] == EXPOSE
    assert [s["step"] for s in config["maintenance"]["steps"]] == [
        "rewrite_data_files",
        "remove_orphan_files",
    ]
    assert config["maintenance"]["steps"][1]["olderThanSeconds"] == 7 * 86400


def test_the_steps_are_the_ones_declared() -> None:
    contract = unwritten(rewriteManifests={}, expireSnapshots={"olderThan": "P5D", "retainLast": 2})
    steps = json.loads(one(contract, account=ACCOUNT).job_text)["maintenance"]["steps"]
    assert [s["step"] for s in steps] == [
        "rewrite_data_files",
        "rewrite_manifests",
        "expire_snapshots",
        "remove_orphan_files",
    ]


def test_the_config_hash_follows_the_declared_maintenance() -> None:
    before = one(account=ACCOUNT)
    after = one(unwritten(expireSnapshots={"olderThan": "P9D"}), account=ACCOUNT)
    assert after.config_hash != before.config_hash
    assert after.job_digest != before.job_digest
    assert one(account=ACCOUNT).config_hash == before.config_hash


def test_binding_the_account_gives_the_run_compiled_with_it() -> None:
    assert one().bind(ACCOUNT).job_text == one(account=ACCOUNT).job_text


def test_a_build_free_contract_with_a_writer_build_still_maintains_through_it() -> None:
    """The ordinary shape is untouched: a build writes the table, no runtime is named."""
    contract = maintained(spark_block())
    (run,) = maintenance_runs(contract, account=ACCOUNT)
    assert run.maintenance_runtime is False
    assert run.build == "ingest-orders"
    assert run.maintenance_actions == ()


# --- the operator's lists ---------------------------------------------------------------


def test_an_unlisted_role_is_refused(monkeypatch) -> None:
    monkeypatch.delenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV)
    with pytest.raises(SparkPolicyError) as caught:
        maintenance_runs(unwritten(), account=ACCOUNT)
    assert isinstance(caught.value, FluidUserError)
    assert ROLE in str(caught.value) or EXPOSE in str(caught.value)


def test_an_unlisted_target_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, "emr-serverless:00f9999999999")
    with pytest.raises(SparkPolicyError):
        maintenance_runs(unwritten(), account=ACCOUNT)


@pytest.mark.parametrize(
    "name", [aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, aws_identity.TARGET_ALLOWLIST_ENV]
)
def test_unset_lists_allow_nothing(monkeypatch, name: str) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(SparkPolicyError):
        maintenance_runs(unwritten(), account=ACCOUNT)


def test_a_plan_takes_the_run_without_the_operators_lists(monkeypatch) -> None:
    monkeypatch.delenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV)
    monkeypatch.delenv(aws_identity.TARGET_ALLOWLIST_ENV)
    run = one(policy=False, account=ACCOUNT)
    assert run.role_arn == ROLE and run.target == {"applicationId": APP}


def test_a_listed_role_prefix_allows_the_role(monkeypatch) -> None:
    monkeypatch.setenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, "arn:aws:iam::123456789012:role/")
    assert one(account=ACCOUNT).role_arn == ROLE


def test_emr_ec2_runs_on_the_named_cluster(monkeypatch) -> None:
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, "emr-ec2:j-1ABCDEF")
    rt = {"runtime": "emr-ec2", "clusterId": "j-1ABCDEF", "executionRoleArn": ROLE}
    run = one(unwritten(rt=rt), account=ACCOUNT)
    assert run.runtime == "emr-ec2" and run.target == {"clusterId": "j-1ABCDEF"}


def test_the_requested_expose_filters_the_runs() -> None:
    assert maintenance_runs(unwritten(), expose_id="other", account=ACCOUNT) == []
    assert len(maintenance_runs(unwritten(), expose_id=EXPOSE, account=ACCOUNT)) == 1


# --- moto: an EMR Serverless submission --------------------------------------------------

boto3 = pytest.importorskip("boto3")
mock_aws = pytest.importorskip("moto").mock_aws

REGION = "eu-west-1"


def clients(service, region):
    return boto3.client(service, region_name=region)


def upload(run, *, runner: str | None = None, body: str | None = None) -> None:
    """The job config and the runner where the run reads them, as OpenTofu uploads them."""
    bucket, _, key = run.job_uri.removeprefix("s3://").partition("/")
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_object(Bucket=bucket, Key=key, Body=(body or run.job_text).encode("utf-8"))
    runner_key = run.runner_uri.removeprefix(f"s3://{bucket}/")
    s3.put_object(Bucket=bucket, Key=runner_key, Body=(runner or runner_source()).encode("utf-8"))


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.mark.emulated
def test_checked_run_accepts_the_uploaded_artifacts_and_start_submits_the_run(aws_env) -> None:
    with mock_aws():
        emr = boto3.client("emr-serverless", region_name=REGION)
        app_id = emr.create_application(name="orders", releaseLabel="emr-7.12.0", type="SPARK")[
            "applicationId"
        ]
        run = one(account=ACCOUNT)
        # moto names the application itself, so the run is moved to the id it made.
        run = dataclasses.replace(run, target={"applicationId": app_id})
        upload(run)

        checked = checked_run(run, clients)
        assert checked.job_sha256  # the digest the runner will hold the config to

        sent: List[Dict[str, Any]] = []

        def recording(service, region):
            client = clients(service, region)
            client.meta.events.register(
                "before-parameter-build.emr-serverless.StartJobRun",
                lambda params, **_: sent.append(dict(params)),
            )
            return client

        job_id = adapter_for(run, recording).start("token-1")
        (request,) = sent
        assert request["applicationId"] == app_id
        assert request["executionRoleArn"] == ROLE
        assert request["name"] == names.maintenance_run_name(PRODUCT, EXPOSE)
        assert request["mode"] == "BATCH"
        assert "retryPolicy" not in request
        driver = request["jobDriver"]["sparkSubmit"]
        assert driver["entryPoint"] == run.runner_uri
        assert driver["entryPointArguments"][:2] == ["--config", run.job_uri]
        assert "--fluid-job-sha256" in driver["entryPointArguments"]
        assert request["tags"]["fluid_maintains"] == EXPOSE
        assert request["tags"]["fluid_build"] == BUILD

        from moto.emrserverless.models import emrserverless_backends

        job = emrserverless_backends[ACCOUNT][REGION].job_runs[app_id][-1]
        assert job.id == job_id
        assert job.execution_role_arn == ROLE
        assert job.name == names.maintenance_run_name(PRODUCT, EXPOSE)


@pytest.mark.emulated
def test_checked_run_refuses_a_config_that_is_not_the_one_compiled(aws_env) -> None:
    with mock_aws():
        run = one(account=ACCOUNT)
        changed = json.loads(run.job_text)
        changed["maintenance"]["steps"].pop()
        upload(run, body=json.dumps(changed))
        with pytest.raises(SparkRunError, match="not the one this apply compiled"):
            checked_run(run, clients)


@pytest.mark.emulated
def test_checked_run_refuses_another_runner(aws_env) -> None:
    with mock_aws():
        run = one(account=ACCOUNT)
        upload(run, runner="print('another runner')\n")
        with pytest.raises(SparkRunError, match="not the one this apply compiled"):
            checked_run(run, clients)


def test_compiling_runs_leaves_the_contract_as_it_was() -> None:
    contract = unwritten()
    before = copy.deepcopy(contract)
    maintenance_runs(contract, account=ACCOUNT)
    assert contract == before
