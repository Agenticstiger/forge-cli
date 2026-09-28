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

"""A Lake Formation grant's column shapes, proven by a real ``tofu plan``.

A grant with ``excludedColumns`` was emitted as ``table_with_columns`` with
``excluded_column_names`` and no ``wildcard``, and the hashicorp/aws provider
refuses that at plan time: "Missing required argument ...
``table_with_columns.0.wildcard``: one of ``column_names,wildcard`` must be
specified". ``tofu validate`` accepted the same module, because the block's
table name is a reference to ``aws_glue_catalog_table`` and the provider skips
the check while a value is unknown. It is known at plan, so plan is the proof.
``TestValidateIsNotTheProof`` pins exactly that, so this file cannot be turned
into a validate-only check without a test going red.

A moto ``ThreadedMotoServer`` answers ``sts:GetCallerIdentity`` for
``data.aws_caller_identity`` (the bucket-policy filter); nothing else is read
at plan time and nothing is applied. No AWS account and no credentials are
involved. Same harness as ``test_iac_lakeformation_bucket_policy_plan.py``.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [pytest.mark.integration, pytest.mark.aws, pytest.mark.provider]

APPLYING_ACCOUNT = "123456789012"
STEWARD = f"arn:aws:iam::{APPLYING_ACCOUNT}:role/steward"
ANALYST = f"arn:aws:iam::{APPLYING_ACCOUNT}:role/analyst"
AUDITOR = f"arn:aws:iam::{APPLYING_ACCOUNT}:role/auditor"
MISSING = "Missing required argument"


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
    """``tofu``'s environment: a shared plugin cache, and no way to reach a real
    account (every ``AWS_*`` variable dropped, moto endpoints, dummy keys)."""
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


def _contract() -> Dict[str, Any]:
    """The demo's bronze shape (a steward with every column, an analyst with a
    pseudonymised column excluded) plus an allow-list grant."""
    grants: List[Dict[str, Any]] = [
        {"principal": STEWARD, "permissions": ["SELECT", "DESCRIBE"]},
        {
            "principal": ANALYST,
            "permissions": ["SELECT", "DESCRIBE"],
            "excludedColumns": ["msisdn"],
        },
        {"principal": AUDITOR, "permissions": ["SELECT"], "columns": ["id", "status"]},
    ]
    return {
        "fluidVersion": "0.7.6",
        "id": "lf.columns",
        "exposes": [
            {
                "exposeId": "customers",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    # No region: the sidecar provider block owns it.
                    "location": {
                        "bucket": "lf-columns-lake",
                        "path": "crm/customers/",
                        "database": "crm",
                        "table": "customers",
                    },
                    "governance": {"lakeFormation": {"registerLocation": True, "grants": grants}},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string"},
                        {"name": "msisdn", "type": "string"},
                        {"name": "status", "type": "string"},
                    ]
                },
            }
        ],
    }


def _write(module: Dict[str, Any], workdir: Path, endpoint: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "main.tf.json").write_text(json.dumps(module))
    (workdir / "provider.tf.json").write_text(json.dumps(_provider_override(endpoint)))


def _tofu(workdir: Path, env: Dict[str, str], *args: str) -> "subprocess.CompletedProcess[str]":
    tofu = runner.tofu_path()
    assert tofu is not None
    return subprocess.run(
        [tofu, *args], cwd=workdir, env=env, capture_output=True, text=True, timeout=600
    )


def _init(workdir: Path, env: Dict[str, str]) -> None:
    # A plugin cache shared by several pytest-xdist workers is not safe for
    # concurrent installs: OpenTofu refuses the per-provider lock instead of
    # waiting. That is contention, not a module error, so it is retried.
    for attempt in range(4):
        done = _tofu(workdir, env, "init", "-backend=false", "-input=false", "-no-color")
        if done.returncode == 0 or "unable to acquire file lock" not in done.stdout + done.stderr:
            break
        time.sleep(2 * (attempt + 1))
    assert done.returncode == 0, f"tofu init failed:\n{done.stdout}\n{done.stderr}"


def _prove(workdir: Path, env: Dict[str, str], *extra: str) -> "subprocess.CompletedProcess[str]":
    """The one command both classes accept a module by. It is ``plan``: were it
    ``validate``, ``TestValidateIsNotTheProof`` would go red."""
    return _tofu(workdir, env, "plan", "-input=false", "-no-color", *extra)


def _emitted() -> Dict[str, Any]:
    return json.loads(build_module(get_iac_plugin("aws"), _contract()))


def _grant_key(module: Dict[str, Any], principal: str) -> str:
    grants = module["resource"]["aws_lakeformation_permissions"]
    (key,) = [k for k, body in grants.items() if body["principal"] == principal]
    return key


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
class TestTheEmittedModulePlans:
    def test_every_column_shape_plans_clean(self, tmp_path, moto_endpoint, tofu_env):
        module = _emitted()
        _write(module, tmp_path, moto_endpoint)
        _init(tmp_path, tofu_env)
        done = _prove(tmp_path, tofu_env, "-out=plan.bin")
        assert done.returncode == 0, f"tofu plan failed:\n{done.stdout}\n{done.stderr}"
        assert MISSING not in done.stdout + done.stderr

        shown = _tofu(tmp_path, tofu_env, "show", "-json", "plan.bin")
        assert shown.returncode == 0, shown.stderr
        planned = {
            change["change"]["after"]["principal"]: change["change"]["after"]
            for change in json.loads(shown.stdout)["resource_changes"]
            if change["type"] == "aws_lakeformation_permissions"
            and change["change"]["actions"] == ["create"]
        }
        assert set(planned) == {STEWARD, ANALYST, AUDITOR}

        # The steward: a table block, every column.
        assert planned[STEWARD]["table"][0]["name"] == "customers"
        assert planned[STEWARD].get("table_with_columns") in (None, [])
        # The analyst: every column except msisdn, which is a column wildcard.
        (analyst,) = planned[ANALYST]["table_with_columns"]
        assert analyst["wildcard"] is True
        assert analyst["excluded_column_names"] == ["msisdn"]
        assert not analyst.get("column_names")
        # The auditor: an allow-list, no wildcard.
        (auditor,) = planned[AUDITOR]["table_with_columns"]
        assert sorted(auditor["column_names"]) == ["id", "status"]
        assert analyst["name"] == auditor["name"] == "customers"
        assert auditor["wildcard"] is False

    def test_a_column_limited_grant_plans_select_only(self, tmp_path, moto_endpoint, tofu_env):
        # Lake Formation takes only SELECT on table_with_columns ("Permissions
        # modification is invalid" otherwise); the plan and moto take anything, so
        # the planned values are what is checked. The analyst asked for SELECT and
        # DESCRIBE; DESCRIBE comes with the column-limited SELECT and is not sent.
        module = _emitted()
        _write(module, tmp_path, moto_endpoint)
        _init(tmp_path, tofu_env)
        done = _prove(tmp_path, tofu_env, "-out=plan.bin")
        assert done.returncode == 0, f"tofu plan failed:\n{done.stdout}\n{done.stderr}"
        shown = _tofu(tmp_path, tofu_env, "show", "-json", "plan.bin")
        assert shown.returncode == 0, shown.stderr
        changes = [
            change
            for change in json.loads(shown.stdout)["resource_changes"]
            if change["type"] == "aws_lakeformation_permissions"
        ]
        # One resource per grant: nothing split off for the same principal.
        assert len(changes) == 3
        by_principal = {change["change"]["after"]["principal"]: change for change in changes}
        for principal in (ANALYST, AUDITOR):
            after = by_principal[principal]["change"]["after"]
            assert after["table_with_columns"], after
            assert after["permissions"] == ["SELECT"], after
        assert sorted(by_principal[STEWARD]["change"]["after"]["permissions"]) == [
            "DESCRIBE",
            "SELECT",
        ]


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
class TestValidateIsNotTheProof:
    """The pre-fix shape (the wildcard stripped from the analyst's grant), run
    through both commands. ``_prove`` must refuse it; ``validate`` accepts it,
    which is why ``_prove`` is a plan."""

    def test_the_pre_fix_shape_passes_validate_and_fails_plan(
        self, tmp_path, moto_endpoint, tofu_env
    ):
        module = _emitted()
        grant = module["resource"]["aws_lakeformation_permissions"][_grant_key(module, ANALYST)]
        # pop, not del: the control is the same module on either side of the fix.
        grant["table_with_columns"][0].pop("wildcard", None)
        assert grant["table_with_columns"][0]["excluded_column_names"] == ["msisdn"]
        _write(module, tmp_path, moto_endpoint)
        _init(tmp_path, tofu_env)

        validated = _tofu(tmp_path, tofu_env, "validate", "-no-color")
        assert validated.returncode == 0, (
            "tofu validate now rejects the pre-fix shape (a provider change); plan "
            f"stays the proof, but revisit this control:\n{validated.stdout}\n{validated.stderr}"
        )

        done = _prove(tmp_path, tofu_env)
        output = done.stdout + done.stderr
        assert done.returncode != 0, "the pre-fix shape must fail the proof"
        assert MISSING in output, output
        assert "table_with_columns.0.wildcard" in output, output
