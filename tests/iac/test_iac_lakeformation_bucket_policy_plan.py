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


def _plan(contract: Dict[str, Any], workdir: Path, endpoint: str, env: Dict[str, str]):
    """``tofu init`` + ``plan``; the planned ``aws_s3_bucket_policy`` instances by address."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), contract))
    (workdir / "provider.tf.json").write_text(json.dumps(_provider_override(endpoint)))
    tofu = runner.tofu_path()
    for args in (
        ["init", "-backend=false", "-input=false", "-no-color"],
        ["plan", "-input=false", "-no-color", "-out=plan.bin"],
    ):
        done = subprocess.run(
            [tofu, *args], cwd=workdir, env=env, capture_output=True, text=True, timeout=600
        )
        assert done.returncode == 0, f"tofu {args[0]} failed:\n{done.stdout}\n{done.stderr}"
    shown = subprocess.run(
        [tofu, "show", "-json", "plan.bin"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    changes = json.loads(shown.stdout)["resource_changes"]
    return {
        change["address"]: json.loads(change["change"]["after"]["policy"])
        for change in changes
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
