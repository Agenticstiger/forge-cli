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

"""The provider-keyed state default, and the move of the old state, against moto.

A bucket-only ``FLUID_STATE_BACKEND`` used to key a contract's state by its
id alone, ``fluid/<id>/terraform.tfstate``, so the aws and the gcp apply of
one contract (two ``--env`` overlays) shared one state and each plan read the
other cloud's resources as orphans to destroy. The default is now
``fluid/<id>/<provider>/terraform.tfstate``, and the first apply after the
upgrade moves the old state there with OpenTofu's own ``init
-migrate-state``.

Everything here is real ``tofu`` against a moto S3 (the state bucket) and
moto AWS APIs (the product's resources): the old release's apply is played
by an apply with the old key spelled out, which is exactly the object that
release wrote. Pinned: the move, a plan after it that changes nothing, the
old object left in place, a second apply that moves nothing, a wiped
workdir (CI) as well as a kept one, the data-loss gate still closed after
the move, another provider's state at the old key left alone, a state of
two clouds refused, and a new key that already holds state never
overwritten.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
import yaml

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import runner
from fluid_build.iac import state_migration as mig
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [pytest.mark.integration, pytest.mark.provider, pytest.mark.aws]

_LOG = logging.getLogger("test.iac.state_key_migration")
_REGION = "us-east-1"
_CID = "bronze.customer_subscriptions"
_STATE_BUCKET = "fluid-state-migration"
_DATA_BUCKET = "migration-moto-lake"
_LEGACY_KEY = f"fluid/{_CID}/terraform.tfstate"
_AWS_KEY = f"fluid/{_CID}/aws/terraform.tfstate"
_GCP_KEY = f"fluid/{_CID}/gcp/terraform.tfstate"


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark.append(
    pytest.mark.skipif(
        runner.tofu_path() is None or not _have_moto_server(),
        reason="needs `tofu` on PATH + moto server extra (pip install 'moto[server]')",
    )
)


@pytest.fixture(scope="module")
def plugin_cache(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One provider download for the module, not one per workdir."""
    return str(tmp_path_factory.mktemp("tofu-plugin-cache"))


@pytest.fixture
def moto(monkeypatch, tmp_path: Path, plugin_cache: str) -> Iterator[str]:
    """A fresh moto server holding the state bucket; tofu and boto3 aim at it."""
    import requests
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"http://127.0.0.1:{port}"
        with contextlib.suppress(Exception):
            requests.post(f"{endpoint}/moto-api/reset", timeout=5)
        for var in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "FLUID_STATE_BACKEND", "FLUID_PROVIDER"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
        monkeypatch.setenv("AWS_REGION", _REGION)
        monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", plugin_cache)
        monkeypatch.chdir(tmp_path)
        _s3(endpoint).create_bucket(Bucket=_STATE_BUCKET)
        yield endpoint
    finally:
        server.stop()


def _s3(endpoint: str):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # pragma: allowlist secret
        region_name=_REGION,
    )


def _object(endpoint: str, key: str) -> Dict[str, Any]:
    body = _s3(endpoint).get_object(Bucket=_STATE_BUCKET, Key=key)["Body"].read()
    return json.loads(body)


def _keys(endpoint: str) -> set:
    listing = _s3(endpoint).list_objects_v2(Bucket=_STATE_BUCKET)
    return {o["Key"] for o in listing.get("Contents", [])}


def _contract(*, table: str = "customer_subscriptions") -> Dict[str, Any]:
    exposes = [
        {
            "exposeId": "subscriptions",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {
                    "database": "demo_bronze",
                    "table": table,
                    "bucket": _DATA_BUCKET,
                    "path": "bronze/customer_subscriptions/",
                },
            },
            "contract": {
                "schema": [
                    {"name": "subscription_id", "type": "VARCHAR", "required": True},
                    {"name": "msisdn", "type": "VARCHAR"},
                ]
            },
        }
    ]
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": _CID,
        "name": "Customer Subscriptions",
        "domain": "Customer",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": exposes,
    }


def _write(root: Path, contract: Dict[str, Any]) -> Path:
    path = root / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _apply(contract_path: Path, root: Path, *, state_backend=None, dry_run=False) -> None:
    args = argparse.Namespace(
        contract=str(contract_path),
        env=None,
        provider=None,
        workspace_dir=str(root),
        state_backend=state_backend,
        dry_run=dry_run,
        allow_data_loss=False,
        no_verify_plan_binding=True,
    )
    assert engine.apply_via_opentofu(args, _LOG) == 0


def _apply_as_the_old_release(contract_path: Path, root: Path) -> None:
    """The object a bucket-only FLUID_STATE_BACKEND wrote before this change."""
    _apply(contract_path, root, state_backend=f"s3://{_STATE_BUCKET}/{_LEGACY_KEY}")


def _upgraded_plan(contract_path: Path, root: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLUID_STATE_BACKEND", f"s3://{_STATE_BUCKET}")
    _apply(contract_path, root, dry_run=True)


def _block(key: str) -> Dict[str, Any]:
    return {"s3": {"bucket": _STATE_BUCKET, "key": key}}


def _reconcile(tmp_path: Path, key: str, provider: str, *, migrate: bool):
    """``reconcile_state_key`` from a workdir initialised on ``key``, as the apply calls it."""
    workdir = tmp_path / f"workdir-{provider}"
    workdir.mkdir(exist_ok=True)
    (workdir / "main.tf.json").write_text(
        json.dumps({"terraform": {"backend": _block(key)}}), encoding="utf-8"
    )
    env = build_tofu_env()
    assert runner.tofu_init(str(workdir), env=env, reconfigure=True).ok
    return mig.reconcile_state_key(
        workdir=workdir,
        current=_block(key),
        legacy=_block(_LEGACY_KEY),
        provider=provider,
        env=env,
        migrate=migrate,
    )


@pytest.mark.parametrize("wipe_workdir", [False, True], ids=["kept-workdir", "wiped-workdir"])
def test_the_old_state_moves_and_the_plan_after_it_changes_nothing(
    moto, tmp_path, monkeypatch, capsys, wipe_workdir
):
    contract = _write(tmp_path, _contract())
    _apply_as_the_old_release(contract, tmp_path)
    old = _object(moto, _LEGACY_KEY)
    assert old["resources"], "the old release's apply wrote no resources"
    assert _AWS_KEY not in _keys(moto)
    if wipe_workdir:
        # A CI workspace is wiped after every run: no .terraform/ records the
        # old key, and the move still has to happen.
        shutil.rmtree(tmp_path / ".fluid")
    capsys.readouterr()

    _upgraded_plan(contract, tmp_path, monkeypatch)

    # The console wraps long lines; the checks read the text unwrapped.
    printed = capsys.readouterr().out.replace("\n", "")
    assert f"remote: s3://{_STATE_BUCKET}/{_AWS_KEY} (from FLUID_STATE_BACKEND)" in printed
    assert f"state move:  moved {len(old['resources'])} resource(s)" in printed
    assert "tofu plan: +0 ~0 -0" in printed
    moved = _object(moto, _AWS_KEY)
    # OpenTofu writes the copy under a fresh lineage; the resources are the same.
    assert moved["resources"] == old["resources"]
    # Never lost: the old object is left exactly where it was.
    assert _object(moto, _LEGACY_KEY) == old

    # The next apply finds its state at the new key and moves nothing.
    _upgraded_plan(contract, tmp_path, monkeypatch)
    again = capsys.readouterr().out.replace("\n", "")
    assert "state move:" not in again
    assert "tofu plan: +0 ~0 -0" in again


def test_the_data_loss_gate_still_closes_after_the_move(moto, tmp_path, monkeypatch):
    """The moved state is the one the gate judges: renaming the table plans a
    destroy, and without --allow-data-loss the apply refuses it."""
    contract = _write(tmp_path, _contract())
    _apply_as_the_old_release(contract, tmp_path)
    monkeypatch.setenv("FLUID_STATE_BACKEND", f"s3://{_STATE_BUCKET}")
    _write(tmp_path, _contract(table="customer_subscriptions_v2"))
    args = argparse.Namespace(
        contract=str(contract),
        env=None,
        provider=None,
        workspace_dir=str(tmp_path),
        state_backend=None,
        dry_run=False,
        allow_data_loss=False,
        no_verify_plan_binding=True,
    )
    with pytest.raises(CLIError) as exc:
        engine.apply_via_opentofu(args, _LOG)
    assert exc.value.event == "opentofu_data_loss_gate"
    assert _AWS_KEY in _keys(moto)


def test_another_providers_state_at_the_old_key_is_left_alone(moto, tmp_path):
    """The gcp apply of a contract whose aws state still sits at the old key:
    it is the aws apply's to move, not the gcp apply's."""
    contract = _write(tmp_path, _contract())
    _apply_as_the_old_release(contract, tmp_path)
    old = _object(moto, _LEGACY_KEY)

    outcome = _reconcile(tmp_path, _GCP_KEY, "gcp", migrate=True)

    assert outcome.outcome == mig.OTHER_PROVIDER
    assert "aws" in outcome.detail
    assert _GCP_KEY not in _keys(moto)
    assert _object(moto, _LEGACY_KEY) == old


def test_a_state_holding_two_clouds_is_refused_and_nothing_moves(moto, tmp_path):
    contract = _write(tmp_path, _contract())
    _apply_as_the_old_release(contract, tmp_path)
    old = _object(moto, _LEGACY_KEY)
    mixed = dict(old)
    mixed["resources"] = list(old["resources"]) + [
        {
            "mode": "managed",
            "type": "google_bigquery_dataset",
            "name": "bronze_customer_subscriptions_demo_bronze",
            "provider": 'provider["registry.opentofu.org/hashicorp/google"]',
            "instances": [],
        }
    ]
    _s3(moto).put_object(
        Bucket=_STATE_BUCKET, Key=_LEGACY_KEY, Body=json.dumps(mixed).encode("utf-8")
    )

    with pytest.raises(mig.StateMigrationError) as exc:
        _reconcile(tmp_path, _AWS_KEY, "aws", migrate=True)

    assert exc.value.code == "state_migration_ambiguous"
    assert "aws, gcp" in str(exc.value)
    assert _AWS_KEY not in _keys(moto)


def test_a_new_key_that_holds_state_is_never_overwritten(moto, tmp_path, monkeypatch):
    contract = _write(tmp_path, _contract())
    # This provider already applied at the new key...
    monkeypatch.setenv("FLUID_STATE_BACKEND", f"s3://{_STATE_BUCKET}")
    _apply(contract, tmp_path)
    current = _object(moto, _AWS_KEY)
    # ...and an older state of this provider still sits at the old one.
    older = dict(current, lineage="00000000-0000-0000-0000-000000000000")
    _s3(moto).put_object(
        Bucket=_STATE_BUCKET, Key=_LEGACY_KEY, Body=json.dumps(older).encode("utf-8")
    )

    outcome = _reconcile(tmp_path, _AWS_KEY, "aws", migrate=True)

    assert outcome.outcome == mig.CURRENT
    assert _object(moto, _AWS_KEY) == current


def test_a_read_only_caller_reads_the_old_key_until_the_apply_moves_it(moto, tmp_path):
    contract = _write(tmp_path, _contract())
    _apply_as_the_old_release(contract, tmp_path)

    outcome = _reconcile(tmp_path, _AWS_KEY, "aws", migrate=False)

    assert outcome.outcome == mig.PENDING
    assert outcome.read_from == _block(_LEGACY_KEY)
    assert _AWS_KEY not in _keys(moto)
