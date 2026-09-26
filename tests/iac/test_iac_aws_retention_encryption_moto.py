# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Retention and encryption at rest through a real ``tofu plan`` / ``apply`` / ``destroy``.

A moto ``ThreadedMotoServer`` stands in for S3, KMS, STS, Glue and Lake
Formation, so no AWS account and no credentials are involved (the harness of
``test_iac_moto_e2e.py`` and ``test_iac_lakeformation_bucket_policy_plan.py``).

* **plan** — the key policy is a ``data.aws_iam_policy_document`` evaluated at
  plan time against the caller identity (moto's account ``123456789012``): the
  account-root statement, the Lake Formation service-linked role's statement,
  and a decrypt statement for the bucket policy's cross-account reader only.
* **apply** — the bucket gets the lifecycle rule and the product key as its
  default SSE-KMS; an object written with no encryption header, as the duckdb
  runner writes, lands SSE-KMS under that key; ``fluid verify``'s storage checks
  pass against the live (emulated) bucket and fail once the rule or an
  object's encryption no longer matches the contract.
* **drop and destroy** — removing the fields from the contract deletes the
  lifecycle configuration and schedules the key's deletion 7 days out; destroy
  leaves nothing.

moto neither enforces key policies nor vends Lake Formation credentials, so
what Athena reading through Lake Formation needs of the key is pinned by the
rendering tests and the AWS documentation, not proven here. Lake Formation is
left out of the apply: moto cannot revoke the grant on destroy.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from fluid_build.cli._verify_storage_policy import storage_policy
from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [pytest.mark.integration, pytest.mark.aws, pytest.mark.provider]

ACCOUNT = "123456789012"  # moto's
REGION = "us-east-1"
SAME = f"arn:aws:iam::{ACCOUNT}:role/analyst"
OTHER = "arn:aws:iam::222222222222:role/other-account-reader"
SLR = (
    f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/lakeformation.amazonaws.com/"
    "AWSServiceRoleForLakeFormationDataAccess"
)
ALIAS = "alias/fluid/retention_moto/retention-moto-lake"
KEY = "aws_kms_key.retention_moto_retention_moto_lake_kms"
LIFECYCLE = "aws_s3_bucket_lifecycle_configuration.retention_moto_retention_moto_lake"


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — Flask (the `server` extra) may be absent
        return False


_SKIP = runner.tofu_path() is None or not _have_moto_server()
_SKIP_REASON = "needs `tofu` on PATH + moto server extra (pip install 'moto[glue,server]')"


@pytest.fixture
def moto_endpoint() -> Iterator[str]:
    """A moto server per test: the apply test mutates what it serves."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture(scope="module")
def tofu_env(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, str]:
    """No way to reach a real account: every ``AWS_*`` variable is dropped, the
    endpoints are moto's and the keys are dummies; a shared plugin cache."""
    env = {k: v for k, v in build_tofu_env().items() if not k.startswith("AWS_")}
    env.setdefault("TF_PLUGIN_CACHE_DIR", str(tmp_path_factory.mktemp("tofu-plugin-cache")))
    env["AWS_CONFIG_FILE"] = "/dev/null"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def _provider_override(endpoint: str) -> Dict[str, Any]:
    services = ("s3", "sts", "iam", "glue", "lakeformation", "kms")
    return {
        "provider": {
            "aws": {
                "region": REGION,
                "access_key": "testing",
                "secret_key": "testing",  # pragma: allowlist secret — moto dummy
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {svc: endpoint for svc in services},
            }
        }
    }


def _contract(
    *,
    grantees: Optional[List[str]] = None,
    retention: bool = True,
    encryption: bool = True,
) -> Dict[str, Any]:
    binding: Dict[str, Any] = {
        "platform": "aws",
        "format": "parquet",
        # No region: the sidecar provider block owns it.
        "location": {
            "bucket": "retention-moto-lake",
            "path": "bronze/orders/",
            "database": "sales",
            "table": "orders",
        },
    }
    if encryption:
        binding["encryption"] = {"kms": "product"}
    if grantees:
        binding["governance"] = {
            "lakeFormation": {
                "registerLocation": True,
                "grants": [{"principal": g, "permissions": ["SELECT"]} for g in grantees],
            }
        }
    exposure: Dict[str, Any] = {
        "exposeId": "orders",
        "binding": binding,
        "contract": {"schema": [{"name": "id", "type": "string"}]},
    }
    if retention:
        exposure["lifecycle"] = {"retention": "P30D", "expire": True}
    return {"fluidVersion": "0.7.6", "id": "retention.moto", "exposes": [exposure]}


def _tofu(workdir: Path, env: Dict[str, str], *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [str(runner.tofu_path()), *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _write(contract: Dict[str, Any], workdir: Path, endpoint: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), contract))
    (workdir / "provider.tf.json").write_text(json.dumps(_provider_override(endpoint)))


def _init(workdir: Path, env: Dict[str, str]) -> None:
    # A plugin cache shared by pytest-xdist workers is not safe for concurrent
    # installs; OpenTofu refuses the lock instead of waiting, so that is retried.
    for attempt in range(4):
        done = _tofu(workdir, env, "init", "-backend=false", "-input=false", "-no-color")
        if done.returncode == 0 or "unable to acquire file lock" not in done.stdout + done.stderr:
            break
        time.sleep(2 * (attempt + 1))
    assert done.returncode == 0, f"tofu init failed:\n{done.stdout}\n{done.stderr}"


def _plan(workdir: Path, env: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """``tofu plan``; the planned changes by address."""
    done = _tofu(workdir, env, "plan", "-input=false", "-no-color", "-out=plan.bin")
    assert done.returncode == 0, f"tofu plan failed:\n{done.stdout}\n{done.stderr}"
    shown = _tofu(workdir, env, "show", "-json", "plan.bin")
    assert shown.returncode == 0, shown.stderr
    return {c["address"]: c for c in json.loads(shown.stdout)["resource_changes"]}


def _apply(workdir: Path, env: Dict[str, str]) -> None:
    done = _tofu(workdir, env, "apply", "-input=false", "-no-color", "plan.bin")
    assert done.returncode == 0, f"tofu apply failed:\n{done.stdout}\n{done.stderr}"


def _boto(service: str, endpoint: str) -> Any:
    import boto3

    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106  # pragma: allowlist secret — moto dummy
        region_name=REGION,
    )


def _check(contract: Dict[str, Any], endpoint: str) -> Dict[str, Dict[str, Any]]:
    """``fluid verify``'s storage checks, against the moto server."""
    exposure = contract["exposes"][0]
    return storage_policy(
        "orders",
        exposure,
        exposure["binding"],
        contract=contract,
        region=REGION,
        factory=lambda service, _region: _boto(service, endpoint),
    ).dimensions


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_the_key_policy_is_evaluated_against_the_applying_account(
    tmp_path, moto_endpoint, tofu_env
):
    _write(_contract(grantees=[SAME, OTHER]), tmp_path, moto_endpoint)
    _init(tmp_path, tofu_env)
    planned = _plan(tmp_path, tofu_env)

    policy = json.loads(planned[KEY]["change"]["after"]["policy"])
    statements = {s["Sid"]: s for s in policy["Statement"]}
    assert set(statements) == {
        "EnableIamPolicies",
        "AllowLakeFormationDataAccess",
        "FluidLfKmsRead1",
    }
    assert statements["EnableIamPolicies"]["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}
    assert statements["EnableIamPolicies"]["Action"] == "kms:*"
    lf = statements["AllowLakeFormationDataAccess"]
    assert lf["Principal"] == {"AWS": "*"}
    assert lf["Condition"] == {"ArnEquals": {"aws:PrincipalArn": SLR}}
    # The same-account analyst reads through Lake Formation and is not in the
    # key policy; the other account's reader may decrypt, through S3 only.
    reader = statements["FluidLfKmsRead1"]
    assert reader["Principal"] == {"AWS": OTHER}
    assert reader["Action"] == "kms:Decrypt"
    assert reader["Condition"] == {"StringLike": {"kms:ViaService": "s3.*.amazonaws.com"}}
    assert SAME not in json.dumps(policy)

    rules = planned[LIFECYCLE]["change"]["after"]["rule"]
    assert [(r["id"], r["filter"][0]["prefix"], r["expiration"][0]["days"]) for r in rules] == [
        ("fluid-retention-orders", "bronze/orders/", 30),
        ("fluid-verify-athena-results", ".fluid/athena-results/", 30),
    ]


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_apply_lands_objects_encrypted_and_verify_holds_the_bucket_to_the_contract(
    tmp_path, moto_endpoint, tofu_env
):
    contract = _contract()
    _write(contract, tmp_path, moto_endpoint)
    _init(tmp_path, tofu_env)
    _plan(tmp_path, tofu_env)
    s3, kms = _boto("s3", moto_endpoint), _boto("kms", moto_endpoint)
    bucket = "retention-moto-lake"
    try:
        _apply(tmp_path, tofu_env)

        # What apply wrote.
        [rule, results_rule] = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        assert rule["Filter"] == {"Prefix": "bronze/orders/"}
        assert rule["Expiration"] == {"Days": 30}
        assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == 1
        assert results_rule["Filter"] == {"Prefix": ".fluid/athena-results/"}
        key = kms.describe_key(KeyId=ALIAS)["KeyMetadata"]
        assert kms.get_key_rotation_status(KeyId=key["KeyId"])["KeyRotationEnabled"] is True
        [default] = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"][
            "Rules"
        ]
        assert default["ApplyServerSideEncryptionByDefault"] == {
            "SSEAlgorithm": "aws:kms",
            "KMSMasterKeyID": key["Arn"],
        }
        assert default["BucketKeyEnabled"] is True

        # An object written with no encryption header, as the duckdb runner
        # writes, is encrypted under the product key.
        s3.put_object(Bucket=bucket, Key="bronze/orders/orders.parquet", Body=b"PAR1")
        head = s3.head_object(Bucket=bucket, Key="bronze/orders/orders.parquet")
        assert head["ServerSideEncryption"] == "aws:kms"
        assert head["SSEKMSKeyId"] == key["Arn"]

        # verify agrees with the bucket...
        checked = _check(contract, moto_endpoint)
        assert checked["retention"]["status"] == "pass", checked
        assert checked["encryption"]["status"] == "pass", checked
        assert checked["encryption"]["actual"]["objects_checked"] == 1

        # ...and fails once it no longer does.
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "fluid-retention-orders",
                        "Filter": {"Prefix": "bronze/orders/"},
                        "Status": "Enabled",
                        "Expiration": {"Days": 90},
                    }
                ]
            },
        )
        s3.put_object(
            Bucket=bucket,
            Key="bronze/orders/late.parquet",
            Body=b"PAR1",
            ServerSideEncryption="AES256",
        )
        checked = _check(contract, moto_endpoint)
        assert checked["retention"]["status"] == "fail"
        assert checked["retention"]["actual"]["days"] == 90
        assert checked["encryption"]["status"] == "fail"
        assert checked["encryption"]["actual"]["wrong"] == [
            {"key": "bronze/orders/late.parquet", "sse": "AES256", "kms_key": None}
        ]
        s3.delete_object(Bucket=bucket, Key="bronze/orders/late.parquet")

        # Dropping both fields: the next apply deletes the lifecycle
        # configuration and schedules the key's deletion, and the bucket stays.
        _write(_contract(retention=False, encryption=False), tmp_path, moto_endpoint)
        planned = _plan(tmp_path, tofu_env)
        assert planned[LIFECYCLE]["change"]["actions"] == ["delete"]
        assert planned[KEY]["change"]["actions"] == ["delete"]
        _apply(tmp_path, tofu_env)
        with pytest.raises(Exception) as excinfo:
            s3.get_bucket_lifecycle_configuration(Bucket=bucket)
        assert "NoSuchLifecycleConfiguration" in str(excinfo.value)
        state = kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]
        assert state["KeyState"] == "PendingDeletion"
        days = (state["DeletionDate"] - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400
        assert 6.9 < days <= 7.0 + 1 / 24, "the minimum window KMS allows"
        with pytest.raises(Exception) as excinfo:
            kms.describe_key(KeyId=ALIAS)
        assert "NotFoundException" in str(excinfo.value)
    finally:
        destroyed = _tofu(
            tmp_path, tofu_env, "destroy", "-auto-approve", "-input=false", "-no-color"
        )
        assert destroyed.returncode == 0, destroyed.stdout + destroyed.stderr
    assert bucket not in [b["Name"] for b in s3.list_buckets()["Buckets"]]
