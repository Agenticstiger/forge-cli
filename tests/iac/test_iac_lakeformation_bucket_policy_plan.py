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

"""The ``bucketPolicy`` filter, evaluated by a real ``tofu plan``.

The default (``cross-account``) cannot be decided when the module is rendered:
which grantee is "in the applying account" depends on the credentials
``tofu`` runs with. So the emit carries the filter as HCL, and the only proof
that it keeps and drops the right statements is a plan that evaluates it.

The same plans prove the two other halves of the bucket policy that only a
plan evaluates: two exposes on one bucket share one policy, with a statement
for each prefix, and on the ``{account}-fluid-data`` fallback bucket the Lake
Formation location and the policy name the applying account.

A moto ``ThreadedMotoServer`` answers ``sts:GetCallerIdentity`` for
``data.aws_caller_identity`` (moto's account is ``123456789012``); the
``aws_arn`` and ``aws_iam_policy_document`` data sources are computed by the
provider locally, and nothing else is read at plan time. No AWS account and
no credentials are involved. Same harness as ``test_iac_moto_e2e.py``.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [pytest.mark.integration, pytest.mark.aws, pytest.mark.provider]

#: moto's default account; the applying account in every plan below.
APPLYING_ACCOUNT = "123456789012"
SAME = f"arn:aws:iam::{APPLYING_ACCOUNT}:role/same-account-reader"
OTHER = "arn:aws:iam::222222222222:role/other-account-reader"
POLICY = "aws_s3_bucket_policy.lf_plan_lf_bucket_policy_lf_plan_lake"


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — Flask (the `server` extra) may be absent
        return False


_SKIP = runner.tofu_path() is None or not _have_moto_server()
_SKIP_REASON = "needs `tofu` on PATH + moto server extra (pip install 'moto[glue,server]')"


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    """One in-process moto server for the module; plans only read from it."""
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
    """``tofu``'s environment: a shared plugin cache (an existing
    ``TF_PLUGIN_CACHE_DIR`` is kept), and no way to reach a real account:
    every ``AWS_*`` variable is dropped, the endpoints are moto's and the keys
    are dummies."""
    env = {k: v for k, v in build_tofu_env().items() if not k.startswith("AWS_")}
    env.setdefault("TF_PLUGIN_CACHE_DIR", str(tmp_path_factory.mktemp("tofu-plugin-cache")))
    env["AWS_CONFIG_FILE"] = "/dev/null"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def _provider_override(endpoint: str) -> Dict[str, Any]:
    services = ("s3", "sts", "iam", "glue", "lakeformation")
    return {
        "provider": {
            "aws": {
                "region": "us-east-1",
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


def _contract(grantees: List[str], bucket_policy: Optional[str] = None) -> Dict[str, Any]:
    lake_formation: Dict[str, Any] = {
        "registerLocation": True,
        "grants": [{"principal": g, "permissions": ["SELECT"]} for g in grantees],
    }
    if bucket_policy is not None:
        lake_formation["bucketPolicy"] = bucket_policy
    return {
        "fluidVersion": "0.7.6",
        "id": "lf.plan",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    # No region: the sidecar provider block owns it.
                    "location": {
                        "bucket": "lf-plan-lake",
                        "path": "orders/",
                        "database": "sales",
                        "table": "orders",
                    },
                    "governance": {"lakeFormation": lake_formation},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _planned_changes(
    contract: Dict[str, Any], workdir: Path, endpoint: str, env: Dict[str, str]
) -> List[Dict[str, Any]]:
    """``tofu init`` + ``plan``; every planned resource change."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), contract))
    (workdir / "provider.tf.json").write_text(json.dumps(_provider_override(endpoint)))
    tofu = runner.tofu_path()

    def run(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [tofu, *args], cwd=workdir, env=env, capture_output=True, text=True, timeout=600
        )

    # A plugin cache shared by several pytest-xdist workers is not safe for
    # concurrent installs: OpenTofu refuses the per-provider lock ("unable to
    # acquire file lock ... resource deadlock avoided") instead of waiting.
    # That is contention, not a module error, so it is retried.
    for attempt in range(4):
        done = run("init", "-backend=false", "-input=false", "-no-color")
        if done.returncode == 0 or "unable to acquire file lock" not in done.stdout + done.stderr:
            break
        time.sleep(2 * (attempt + 1))
    assert done.returncode == 0, f"tofu init failed:\n{done.stdout}\n{done.stderr}"
    done = run("plan", "-input=false", "-no-color", "-out=plan.bin")
    assert done.returncode == 0, f"tofu plan failed:\n{done.stdout}\n{done.stderr}"
    shown = subprocess.run(
        [tofu, "show", "-json", "plan.bin"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    return list(json.loads(shown.stdout)["resource_changes"])


def _plan(contract: Dict[str, Any], workdir: Path, endpoint: str, env: Dict[str, str]):
    """The planned ``aws_s3_bucket_policy`` instances by address, policies parsed."""
    return {
        change["address"]: json.loads(change["change"]["after"]["policy"])
        for change in _planned_changes(contract, workdir, endpoint, env)
        if change["type"] == "aws_s3_bucket_policy" and change["change"]["actions"] == ["create"]
    }


def _grantees(policy: Dict[str, Any]) -> Dict[str, str]:
    return {s["Sid"]: s["Principal"]["AWS"] for s in policy["Statement"]}


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
class TestTheFilterAtPlanTime:
    def test_a_same_account_grantee_gets_no_bucket_policy(self, tmp_path, moto_endpoint, tofu_env):
        planned = _plan(_contract([SAME]), tmp_path, moto_endpoint, tofu_env)
        assert planned == {}, "a same-account grantee must not be written into the bucket policy"

    def test_a_cross_account_grantee_keeps_its_statements(self, tmp_path, moto_endpoint, tofu_env):
        planned = _plan(_contract([SAME, OTHER]), tmp_path, moto_endpoint, tofu_env)
        assert list(planned) == [f"{POLICY}[0]"]
        # Only the other-account grantee, under its all-grantees Sid index.
        assert _grantees(planned[f"{POLICY}[0]"]) == {
            "FluidLfBucketList1": OTHER,
            "FluidLfBucketGet1": OTHER,
        }
        statements = {s["Sid"]: s for s in planned[f"{POLICY}[0]"]["Statement"]}
        assert statements["FluidLfBucketGet1"]["Resource"] == "arn:aws:s3:::lf-plan-lake/orders/*"
        assert statements["FluidLfBucketList1"]["Resource"] == "arn:aws:s3:::lf-plan-lake"

    def test_all_grantees_restores_the_same_account_statement(
        self, tmp_path, moto_endpoint, tofu_env
    ):
        planned = _plan(_contract([SAME, OTHER], "all-grantees"), tmp_path, moto_endpoint, tofu_env)
        assert list(planned) == [POLICY]
        assert _grantees(planned[POLICY]) == {
            "FluidLfBucketList0": SAME,
            "FluidLfBucketGet0": SAME,
            "FluidLfBucketList1": OTHER,
            "FluidLfBucketGet1": OTHER,
        }

    def test_none_plans_no_bucket_policy(self, tmp_path, moto_endpoint, tofu_env):
        planned = _plan(_contract([SAME, OTHER], "none"), tmp_path, moto_endpoint, tofu_env)
        assert planned == {}


def _two_zones(
    raw: List[str], curated: List[str], bucket_policy: Optional[str] = None
) -> Dict[str, Any]:
    """Two exposes on the bucket ``lf-plan-lake``, as in examples/aws-medallion-lake."""
    contract = _contract(raw, bucket_policy)
    contract["exposes"][0]["binding"]["location"]["path"] = "raw/"
    zone = _contract(curated, bucket_policy)["exposes"][0]
    zone["exposeId"] = "curated"
    zone["binding"]["location"].update({"path": "curated/", "database": "silver"})
    contract["exposes"].append(zone)
    return contract


def _statements(policy: Dict[str, Any]) -> Dict[str, Any]:
    return {s["Sid"]: (s["Principal"]["AWS"], s["Resource"]) for s in policy["Statement"]}


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
class TestOneBucketTwoExposesAtPlanTime:
    """Each expose used to emit the bucket's policy under the same key, so the
    second replaced the first and ``raw/`` lost its cross-account statements."""

    def test_both_prefixes_keep_their_cross_account_statements(
        self, tmp_path, moto_endpoint, tofu_env
    ):
        planned = _plan(_two_zones([SAME, OTHER], [OTHER]), tmp_path, moto_endpoint, tofu_env)
        assert list(planned) == [f"{POLICY}[0]"], "one policy for the one bucket"
        assert _statements(planned[f"{POLICY}[0]"]) == {
            "FluidLfBucketList1": (OTHER, "arn:aws:s3:::lf-plan-lake"),
            "FluidLfBucketGet1": (OTHER, "arn:aws:s3:::lf-plan-lake/raw/*"),
            "FluidLfBucketList2": (OTHER, "arn:aws:s3:::lf-plan-lake"),
            "FluidLfBucketGet2": (OTHER, "arn:aws:s3:::lf-plan-lake/curated/*"),
        }

    def test_same_account_grantees_on_both_exposes_plan_no_policy(
        self, tmp_path, moto_endpoint, tofu_env
    ):
        planned = _plan(_two_zones([SAME], [SAME]), tmp_path, moto_endpoint, tofu_env)
        assert planned == {}

    def test_all_grantees_writes_both_prefixes(self, tmp_path, moto_endpoint, tofu_env):
        contract = _two_zones([SAME], [OTHER], "all-grantees")
        planned = _plan(contract, tmp_path, moto_endpoint, tofu_env)
        assert list(planned) == [POLICY]
        assert _statements(planned[POLICY]) == {
            "FluidLfBucketList0": (SAME, "arn:aws:s3:::lf-plan-lake"),
            "FluidLfBucketGet0": (SAME, "arn:aws:s3:::lf-plan-lake/raw/*"),
            "FluidLfBucketList1": (OTHER, "arn:aws:s3:::lf-plan-lake"),
            "FluidLfBucketGet1": (OTHER, "arn:aws:s3:::lf-plan-lake/curated/*"),
        }


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
class TestTheFallbackBucketAtPlanTime:
    """An unresolved ``location.bucket`` falls back to ``{account}-fluid-data``.
    The Lake Formation location was planned as the literal
    ``arn:aws:s3:::${data.aws_caller_identity...}-fluid-data/...``, and a
    bucket policy failed the plan with "Reference to undeclared resource"."""

    ACCOUNT_BUCKET = f"{APPLYING_ACCOUNT}-fluid-data"

    @pytest.fixture(autouse=True)
    def _unset(self, monkeypatch):
        monkeypatch.delenv("FLUID_TEST_LF_PLAN_BUCKET", raising=False)

    def _fallback(self, bucket_policy: Optional[str] = None) -> Dict[str, Any]:
        contract = _contract([OTHER], bucket_policy)
        contract["exposes"][0]["binding"]["location"][
            "bucket"
        ] = "{{ env.FLUID_TEST_LF_PLAN_BUCKET }}"
        return contract

    @pytest.mark.parametrize("bucket_policy", [None, "all-grantees"])
    def test_the_location_and_the_policy_name_the_applying_account(
        self, bucket_policy, tmp_path, moto_endpoint, tofu_env
    ):
        changes = _planned_changes(self._fallback(bucket_policy), tmp_path, moto_endpoint, tofu_env)
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for change in changes:
            by_type.setdefault(change["type"], []).append(change["change"]["after"])
        assert [r["arn"] for r in by_type["aws_lakeformation_resource"]] == [
            f"arn:aws:s3:::{self.ACCOUNT_BUCKET}/orders/"
        ]
        (policy,) = by_type["aws_s3_bucket_policy"]
        assert policy["bucket"] == self.ACCOUNT_BUCKET
        resources = {s["Resource"] for s in json.loads(policy["policy"])["Statement"]}
        assert resources == {
            f"arn:aws:s3:::{self.ACCOUNT_BUCKET}",
            f"arn:aws:s3:::{self.ACCOUNT_BUCKET}/orders/*",
        }
