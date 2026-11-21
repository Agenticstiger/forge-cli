# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A prebuilt package build on AWS: OpenTofu copies the pinned package beside the job config
only when S3's SHA-256 of it is the pin, the job reads that copy alone, and its dead letters
get a table of their own shape."""

from __future__ import annotations

import base64
import copy
import json

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import aws_identity, aws_ingest
from fluid_build.spark import dead_letter, names
from fluid_build.spark.package import PACKAGE_ALLOWLIST_ENV
from fluid_build.spark.policy import SparkPolicyError
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.spark.package_fixtures import PACKAGE_CONTRACT, SHA, SOURCE

pytestmark = [pytest.mark.unit, pytest.mark.provider]

KEY = "commerce_orders_silver_merge_orders_spark"
CLUSTER = "j-2AXXXXXXGAPLF"
APP = "00fabc123def"  # pragma: allowlist secret - an id
SOURCE_KEY = SOURCE.removeprefix("s3://commerce-artifacts/")
B64 = base64.b64encode(bytes.fromhex(SHA)).decode()


@pytest.fixture(autouse=True)
def operator(monkeypatch):
    for name in (
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV,
        aws_identity.SPARK_CONF_ALLOWLIST_ENV,
        HOST_ALLOWLIST_ENV,
        "FLUID_SPARK_CONTRACT_SQL_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, f"emr-ec2:{CLUSTER},emr-serverless:{APP}")
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, "s3://commerce-artifacts/code/")


@pytest.fixture
def contract():
    return copy.deepcopy(PACKAGE_CONTRACT)


ROLE = "arn:aws:iam::000000000005:role/data/orders-silver"


def serverless(contract, monkeypatch):
    """On EMR Serverless, with the operator's role: a package reads the tables it names."""
    monkeypatch.setenv(
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, "arn:aws:iam::000000000005:role/data/"
    )
    contract["builds"][0]["properties"]["parameters"]["spark"] = {
        "runtime": "emr-serverless",
        "applicationId": APP,
        "mode": "batch",
        "executionRoleArn": ROLE,
    }
    return contract


def emit(contract):
    plugin = get_iac_plugin("aws")
    return plugin.emit(contract), plugin.emit_data(contract)


def test_the_pinned_package_is_copied_beside_the_job_config(contract):
    resources, data = emit(contract)
    name = f"{KEY}_package"
    assert data["aws_s3_object"][name] == {
        "bucket": "commerce-artifacts",
        "key": SOURCE_KEY,
        "checksum_mode": "ENABLED",
    }
    copied = resources["aws_s3_object_copy"][name]
    assert copied["key"] == names.package_key("commerce.orders-silver", "merge-orders", SHA)
    # The output's bucket, by reference, beside the job config and the runner.
    assert copied["bucket"] == resources["aws_s3_object"][f"{KEY}_config"]["bucket"]
    assert copied["source"] == f"commerce-artifacts/{SOURCE_KEY}"
    # S3 copies the bytes the plan verified, or nothing, and checks the copy.
    assert copied["copy_if_match"] == f"${{data.aws_s3_object.{name}.etag}}"
    assert copied["checksum_algorithm"] == "SHA256"
    assert copied["tagging_directive"] == "REPLACE"
    (pre,) = copied["lifecycle"]["precondition"]
    assert pre["condition"] == f'${{data.aws_s3_object.{name}.checksum_sha256 == "{B64}"}}'
    assert "single-part" in pre["error_message"] and SHA in pre["error_message"]
    (post,) = copied["lifecycle"]["postcondition"]
    assert post["condition"] == f'${{self.checksum_sha256 == "{B64}"}}'


def test_the_job_config_names_the_digest_and_never_the_store(contract):
    resources, _ = emit(contract)
    text = resources["aws_s3_object"][f"{KEY}_config"]["content"]
    config = json.loads(text)
    assert config["package"]["sha256"] == SHA
    assert "commerce-artifacts" not in text


def test_the_job_role_reads_its_own_prefix_and_never_the_store(contract):
    """The policy forge writes for the cluster's role (emr-ec2) grants the copy, never the
    team's store."""
    resources, data = emit(contract)
    assert resources["aws_iam_policy"][KEY]["policy"] == (
        f"${{data.aws_iam_policy_document.{KEY}_access.json}}"
    )
    access = json.dumps(data["aws_iam_policy_document"][f"{KEY}_access"])
    assert "commerce-artifacts" not in access
    assert "_fluid/spark/commerce.orders-silver/merge-orders/*" in access


def test_a_role_forge_derives_cannot_read_what_a_package_reads(contract, monkeypatch):
    params = serverless(contract, monkeypatch)["builds"][0]["properties"]["parameters"]["spark"]
    params.pop("executionRoleArn")
    with pytest.raises(UnsupportedBindingError, match="tables its package names"):
        emit(contract)


def test_the_dead_letters_get_a_table_of_their_own_shape(contract):
    resources, data = emit(contract)
    tables = resources["aws_glue_catalog_table"]
    (dead,) = [t for t in tables.values() if t["name"] == "orders__dead_letter"]
    columns = [c["name"] for c in dead["storage_descriptor"]["columns"]]
    assert columns == [name for name, _, _ in dead_letter.BATCH_COLUMNS]
    assert "merge-orders" in dead["description"] and "package" in dead["description"]
    glue = aws_ingest_statement(data, "GlueTable")
    assert any(r.endswith("table/commerce/orders__dead_letter") for r in glue["resources"])
    access = json.dumps(data["aws_iam_policy_document"][f"{KEY}_access"])
    assert "iceberg/commerce/orders__dead_letter/*" in access


def test_the_operator_s_role_commits_the_dead_letters(contract, monkeypatch):
    """Where Lake Formation governs, the job's role is granted the dead letters' table."""
    resources, _ = emit(serverless(contract, monkeypatch))
    names_ = [t["name"] for t in resources["aws_glue_catalog_table"].values()]
    assert sorted(names_) == ["orders", "orders__dead_letter"]
    assert resources["aws_s3_object_copy"][f"{KEY}_package"]["key"].endswith("/package.zip")


def aws_ingest_statement(data, sid):
    document = data["aws_iam_policy_document"][f"{KEY}_access"]
    return next(s for s in document["statement"] if s.get("sid") == sid)


def test_without_dead_letters_there_is_no_table(contract):
    contract["builds"][0]["properties"]["package"].pop("deadLetter")
    resources, _ = emit(contract)
    names_ = [t["name"] for t in resources["aws_glue_catalog_table"].values()]
    assert names_ == ["orders"]


def test_the_copy_holds_no_data(contract):
    """Replacing or dropping a package copy is not data loss."""
    assert aws_ingest.spark_definition(f"aws_s3_object_copy.{KEY}_package", "aws_s3_object_copy")
    assert not aws_ingest.spark_definition(f"aws_s3_object_copy.{KEY}_other", "aws_s3_object_copy")


def test_an_unlisted_store_is_refused_before_anything_is_emitted(contract, monkeypatch):
    monkeypatch.delenv(PACKAGE_ALLOWLIST_ENV)
    with pytest.raises(SparkPolicyError):
        emit(contract)


def test_an_argument_holding_an_interpolation_is_refused(contract):
    contract["builds"][0]["properties"]["package"]["arguments"] = {"day": "${ds}"}
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-config-interpolation"
