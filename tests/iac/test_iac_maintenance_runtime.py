# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The OpenTofu half of ``exposes[].iceberg.maintenance.runtime`` (a table no forge build writes,
``iac/providers/aws_ingest.py::_emit_unwritten_maintenance``).

It uploads two objects to the table's bucket (the runner and the maintenance job config, under
``maintain_<expose>``) and lets the scheduler's caller policy start runs on the runtime the block
names, pass the role it names and read the two objects. It provisions no log group, no role and
no EMR application: those are the team's.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict

import pytest

from fluid_build.build_runners.spark import _desired
from fluid_build.build_runners.spark._maintenance import maintenance_runs
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.module import build_module
from fluid_build.iac.providers import aws_identity, aws_ingest
from fluid_build.spark import names, runner_source
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from fluid_build.spark.policy import SparkPolicyError
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.iac.test_iac_aws_spark_infra import CONTRACT as INFRA_CONTRACT
from tests.iac.test_iac_aws_spark_infra import KEY as BUILD_KEY
from tests.iac.test_iac_aws_spark_infra import MSK_ARN

pytestmark = [pytest.mark.unit, pytest.mark.provider]

ACCOUNT = "123456789012"
ACCOUNT_REF = "${data.aws_caller_identity.fluid_lf_caller.account_id}"
ROLE = "arn:aws:iam::123456789012:role/orders-maint"
APP = "00f1abcd2345"
PRODUCT = "commerce.orders-stream"
BUCKET = "commerce-lake"
BUCKET_REF = "${aws_s3_bucket.commerce_orders_stream_commerce_lake.id}"
RUNNER_KEY = "commerce_orders_stream_maintain_order_lines_spark_runner"
CONFIG_KEY = "commerce_orders_stream_maintain_order_lines_spark_maintain_order_lines"
CALLER = "commerce_orders_stream_spark_caller"
PREFIX = f"_fluid/spark/{PRODUCT}/maintain_order_lines"
CONFIG_OBJECT = f"{PREFIX}/maintenance/order_lines/job.json"


def block(**extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "schedule": "0 3 * * *",
        "runtime": {"runtime": "emr-serverless", "applicationId": APP, "executionRoleArn": ROLE},
        "compaction": {},
        "expireSnapshots": {"olderThan": "P5D"},
        "removeOrphanFiles": {"olderThan": "P7D"},
    }
    out.update(extra)
    return out


@pytest.fixture(autouse=True)
def operator_lists(monkeypatch):
    """The operator lists the example's role and application, and nothing else."""
    for name in (
        CONTRACT_SQL_ALLOWLIST_ENV,
        aws_identity.SPARK_CONF_ALLOWLIST_ENV,
        HOST_ALLOWLIST_ENV,
        "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, ROLE)
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, f"emr-serverless:{APP}")


@pytest.fixture
def unwritten() -> Dict[str, Any]:
    """The Spark example with no build: its team's own streams write ``order_lines``."""
    contract = copy.deepcopy(INFRA_CONTRACT)
    contract["builds"] = []
    contract["exposes"][0]["iceberg"] = {"maintenance": block()}
    return contract


@pytest.fixture
def written(monkeypatch) -> Dict[str, Any]:
    """The same contract whose build writes the table, with no maintenance (and the operator
    listing that build's own application as well)."""
    monkeypatch.setenv(
        aws_identity.TARGET_ALLOWLIST_ENV,
        f"emr-serverless:{APP},emr-serverless:00fabc123def" f",msk:{MSK_ARN}",
    )
    return copy.deepcopy(INFRA_CONTRACT)


def emit(contract):
    plugin = get_iac_plugin("aws")
    return plugin.emit(contract), plugin.emit_data(contract)


def statement(document, sid):
    return next(s for s in document["statement"] if s.get("sid") == sid)


# --- the uploaded objects ----------------------------------------------------------------


def test_the_runner_and_the_config_are_uploaded_to_the_tables_bucket(unwritten) -> None:
    resources, _ = emit(unwritten)
    objects = resources["aws_s3_object"]
    assert sorted(objects) == [CONFIG_KEY, RUNNER_KEY]
    sha = names.runner_sha256(runner_source())
    runner, config = objects[RUNNER_KEY], objects[CONFIG_KEY]
    assert runner["key"] == f"{PREFIX}/runner/{sha[:16]}/forge_spark_runner.py"
    assert runner["content"] == runner_source()
    assert runner["content_type"] == "text/x-python"
    assert config["key"] == CONFIG_OBJECT
    assert config["content_type"] == "application/json"
    assert runner["bucket"] == config["bucket"] == BUCKET_REF


def test_the_objects_are_tagged_with_what_they_maintain(unwritten) -> None:
    objects = emit(unwritten)[0]["aws_s3_object"]
    for obj in objects.values():
        assert obj["tags"]["fluid_product"] == PRODUCT
        assert obj["tags"]["fluid_build"] == "maintain_order_lines"
        assert obj["tags"]["fluid_maintains"] == "order_lines"


def test_the_uploaded_config_is_the_one_apply_compiles_for_the_table(unwritten) -> None:
    resources, _ = emit(unwritten)
    text = str(resources["aws_s3_object"][CONFIG_KEY]["content"]).replace(ACCOUNT_REF, ACCOUNT)
    (run,) = maintenance_runs(unwritten, account=ACCOUNT)
    assert "${" not in text
    assert _desired.same_config(text.encode("utf-8"), run.job_text)
    assert run.config_matches(text.encode("utf-8"))
    assert run.job_uri == f"s3://{BUCKET}/{CONFIG_OBJECT}"
    assert resources["aws_s3_object"][RUNNER_KEY]["key"] == run.runner_uri.removeprefix(
        f"s3://{BUCKET}/"
    )
    changed = json.loads(text)
    changed["maintenance"]["steps"].pop()
    assert not run.config_matches(json.dumps(changed).encode("utf-8"))


def test_the_config_is_a_maintenance_job_with_the_declared_steps(unwritten) -> None:
    text = emit(unwritten)[0]["aws_s3_object"][CONFIG_KEY]["content"]
    config = json.loads(str(text).replace(ACCOUNT_REF, ACCOUNT))
    assert config["mode"] == "maintenance" and config["build"] == "maintain_order_lines"
    assert config["sink"]["table"] == "commerce.order_lines"
    assert [s["step"] for s in config["maintenance"]["steps"]] == [
        "rewrite_data_files",
        "expire_snapshots",
        "remove_orphan_files",
    ]


def test_a_changed_block_changes_the_config_and_not_the_runner(unwritten) -> None:
    before = emit(unwritten)[0]["aws_s3_object"]
    unwritten["exposes"][0]["iceberg"]["maintenance"]["expireSnapshots"] = {"olderThan": "P9D"}
    after = emit(unwritten)[0]["aws_s3_object"]
    assert after[CONFIG_KEY] != before[CONFIG_KEY]
    assert after[RUNNER_KEY] == before[RUNNER_KEY]


def test_the_rendered_module_holds_only_forges_own_interpolations(unwritten) -> None:
    rendered = json.loads(build_module(get_iac_plugin("aws"), unwritten))
    content = rendered["resource"]["aws_s3_object"][CONFIG_KEY]["content"]
    # nothing of the contract's own text is interpolated; the account is the one reference
    assert content.replace(ACCOUNT_REF, "").count("${") == 0


def test_the_output_is_stable(unwritten) -> None:
    plugin = get_iac_plugin("aws")
    assert build_module(plugin, unwritten) == build_module(plugin, copy.deepcopy(unwritten))


# --- what it does not provision ----------------------------------------------------------


def test_no_log_group_role_or_application_is_emitted(unwritten) -> None:
    resources, data = emit(unwritten)
    assert "aws_cloudwatch_log_group" not in resources
    assert "aws_iam_role" not in resources
    assert "aws_iam_role_policy" not in resources
    assert not any("emrserverless" in k or "emr" in k.split("_")[1:2] for k in resources)
    assert set(resources) == {
        "aws_glue_catalog_database",
        "aws_glue_catalog_table",
        "aws_iam_policy",
        "aws_s3_bucket",
        "aws_s3_object",
    }
    assert "aws_iam_role" not in data


# --- the scheduler's caller policy -------------------------------------------------------


def test_the_caller_may_start_runs_on_the_named_application(unwritten) -> None:
    resources, data = emit(unwritten)
    assert CALLER in resources["aws_iam_policy"]
    document = data["aws_iam_policy_document"][CALLER]
    run = statement(document, "RunEmrServerless")
    assert "emr-serverless:StartJobRun" in run["actions"]
    assert all(f"/applications/{APP}" in r for r in run["resources"])
    assert len(run["resources"]) == 2


def test_the_caller_may_pass_only_the_named_role(unwritten) -> None:
    document = emit(unwritten)[1]["aws_iam_policy_document"][CALLER]
    passed = statement(document, "PassRoleEmrServerless")
    assert passed["actions"] == ["iam:PassRole"]
    assert passed["resources"] == [ROLE]
    (condition,) = passed["condition"]
    assert condition["values"] == ["emr-serverless.amazonaws.com"]


def test_the_caller_may_read_the_two_objects_and_nothing_else_in_the_bucket(unwritten) -> None:
    document = emit(unwritten)[1]["aws_iam_policy_document"][CALLER]
    artifacts = statement(document, "Artifacts")
    assert artifacts["actions"] == ["s3:GetObject"]
    sha = names.runner_sha256(runner_source())
    assert sorted(artifacts["resources"]) == [
        f"arn:aws:s3:::{BUCKET}/{CONFIG_OBJECT}",
        f"arn:aws:s3:::{BUCKET}/{PREFIX}/runner/{sha[:16]}/forge_spark_runner.py",
    ]


@pytest.mark.parametrize(
    ("rt", "listed"),
    [
        (
            {"runtime": "emr-ec2", "clusterId": "j-1ABCDEF", "executionRoleArn": ROLE},
            "emr-ec2:j-1ABCDEF",
        ),
    ],
)
def test_emr_ec2_names_its_cluster_in_the_caller_policy(unwritten, monkeypatch, rt, listed) -> None:
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, listed)
    unwritten["exposes"][0]["iceberg"]["maintenance"]["runtime"] = rt
    document = emit(unwritten)[1]["aws_iam_policy_document"][CALLER]
    assert "j-1ABCDEF" in json.dumps(statement(document, "RunEmrEc2"))
    assert any(r.endswith(CONFIG_OBJECT) for r in statement(document, "Artifacts")["resources"])


# --- the operator's lists ----------------------------------------------------------------


def test_an_unlisted_role_is_refused_at_emit(unwritten, monkeypatch) -> None:
    monkeypatch.setenv(
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, "arn:aws:iam::123456789012:role/other"
    )
    with pytest.raises(SparkPolicyError):
        emit(unwritten)


def test_an_unlisted_target_is_refused_at_emit(unwritten, monkeypatch) -> None:
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, "emr-serverless:00f9999999999")
    with pytest.raises(SparkPolicyError):
        emit(unwritten)


@pytest.mark.parametrize(
    "name", [aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, aws_identity.TARGET_ALLOWLIST_ENV]
)
def test_unset_lists_refuse_emit(unwritten, monkeypatch, name) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(SparkPolicyError):
        emit(unwritten)


# --- a table a build writes is as it was -------------------------------------------------


def test_a_contract_whose_build_writes_its_table_gets_no_maintenance_objects(written) -> None:
    resources, _ = emit(written)
    assert not any("maintain_" in k for k in resources["aws_s3_object"])
    assert f"{BUILD_KEY}_config" in resources["aws_s3_object"]


def test_a_contract_without_builds_or_the_block_gets_nothing() -> None:
    contract = copy.deepcopy(INFRA_CONTRACT)
    contract.pop("builds")
    assert aws_ingest.spark_blocks(contract, "x") == aws_ingest.SparkBlocks({}, {})


def test_a_build_named_block_adds_nothing_beside_the_builds_objects(written) -> None:
    plain = emit(copy.deepcopy(written))[0]
    written["exposes"][0]["iceberg"] = {"maintenance": {"schedule": "0 3 * * *", "compaction": {}}}
    maintained = emit(written)[0]
    assert not any("maintain_order_lines_spark" in k for k in maintained["aws_s3_object"])
    assert plain["aws_cloudwatch_log_group"] == maintained["aws_cloudwatch_log_group"]
    assert plain["aws_iam_role"] == maintained["aws_iam_role"]
