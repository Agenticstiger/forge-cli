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

"""Stage 1 — ``fluid apply`` OpenTofu engine against moto, for AWS contracts.

These tests drive the real CLI entrypoint (``apply_via_opentofu``) — not
just the plugin — so the full pipeline is exercised: contract → ``.tf.json``
→ ``tofu init/plan/apply``, including the data-loss gate and ``--dry-run``.

A moto ``ThreadedMotoServer`` provides the AWS API surface; a sidecar
``provider.tf.json`` aiming the AWS provider at moto is dropped into the
engine's own workdir (``<workspace_dir>/.fluid/iac/aws/<id>/``) before
apply, so ``tofu init`` merges it with the plugin's credential-free
``main.tf.json``. Skipped unless ``tofu`` is on PATH and moto's ``server``
extra is installed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import runner
from fluid_build.iac.naming import safe_ident

pytestmark = [pytest.mark.integration, pytest.mark.provider, pytest.mark.aws]


_LOG = logging.getLogger("test.iac.aws.engine")
_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Skip + fixtures
# ---------------------------------------------------------------------------


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


_SKIP = runner.tofu_path() is None or not _have_moto_server()
_SKIP_REASON = "needs `tofu` on PATH + moto server extra (pip install 'moto[server]')"


@pytest.fixture
def moto_endpoint() -> Iterator[str]:
    """Start a fresh moto AWS server and clear its process-wide state.

    moto's ``ThreadedMotoServer`` shares state with every other moto
    process-level mock — stopping and starting a new server does NOT clear
    the backends. The ``/moto-api/reset`` admin endpoint clears all state,
    giving each test the clean account it expects.
    """
    import requests
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"http://127.0.0.1:{port}"
        # Reset before yielding — also covers state leaked in by a previous
        # test running in the same process.
        with contextlib.suppress(Exception):
            requests.post(f"{endpoint}/moto-api/reset", timeout=5)
        yield endpoint
    finally:
        server.stop()


def _provider_override(endpoint: str) -> Dict[str, Any]:
    services = ("s3", "glue", "sts", "iam", "kinesis", "lambda", "events")
    return {
        "provider": {
            "aws": {
                "region": _REGION,
                "access_key": "testing",
                "secret_key": "testing",
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {svc: endpoint for svc in services},
            }
        }
    }


def _boto(service: str, endpoint: str):
    import boto3

    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 — dummy, moto only
        region_name=_REGION,
    )


def _aws_iceberg_contract(bucket: str, cid: str = "iac.engine.aws") -> Dict[str, Any]:
    return {
        "id": cid,
        "name": "Engine AWS Iceberg",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": "engine_silver",
                        "table": "events",
                        "bucket": bucket,
                        "path": "silver/events/",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "bigint", "required": True}]},
            }
        ],
    }


def _write_plan(path: Path, contract: Dict[str, Any]) -> Path:
    """Write a contract as a ``plan.json`` — the engine loads it without
    going through the schema validator, keeping these tests scoped to the
    apply engine itself."""
    path.write_text(json.dumps({"contract": contract}), encoding="utf-8")
    return path


def _apply_args(contract_path: Path, workspace_dir: Path, **overrides) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "contract": str(contract_path),
        "env": None,
        "provider": None,
        "workspace_dir": str(workspace_dir),
        "state_backend": None,
        "dry_run": False,
        "allow_data_loss": False,
        # Synthetic plan.json written by these tests carries ``{"contract": ...}``
        # only — no bundle, no actions, no bindingMode. The OpenTofu engine's
        # plan-binding gate (added by commit 4c9163f) would otherwise reject
        # with ``apply_plan_digest_binding_mode_missing``. The real plan→apply
        # binding chain is pinned by ``test_iac_aws_real_cli_matrix_e2e.py``
        # (live AWS) and ``test_iac_snowflake_real_cli_matrix_e2e.py`` (live
        # Snowflake); this file exercises the moto-backed resource-emit path,
        # so the bypass is correct.
        "no_verify_plan_binding": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _drop_moto_provider_sidecar(workspace_dir: Path, contract_id: str, endpoint: str) -> Path:
    """Drop ``provider.tf.json`` into the apply engine's workdir BEFORE the
    engine runs — ``tofu init`` merges every ``*.tf.json`` in the directory,
    so this overlays the moto endpoint + dummy creds onto the plugin's
    credential-free ``main.tf.json``."""
    workdir = workspace_dir / ".fluid" / "iac" / "aws" / safe_ident(contract_id)
    workdir.mkdir(parents=True, exist_ok=True)
    sidecar = workdir / "provider.tf.json"
    sidecar.write_text(json.dumps(_provider_override(endpoint)), encoding="utf-8")
    return workdir


# ---------------------------------------------------------------------------
# Unit — engine resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_apply_engine_is_opentofu_for_aws(tmp_path):
    """An AWS contract resolves to the OpenTofu engine (cutover default)."""
    plan = _write_plan(tmp_path / "plan.json", _aws_iceberg_contract("bucket-x"))
    args = argparse.Namespace(contract=str(plan), env=None, provider=None)
    assert engine.resolve_apply_engine(args, _LOG) == "opentofu"


# ---------------------------------------------------------------------------
# Live (moto) — apply engine end-to-end for AWS
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_apply_engine_provisions_iceberg_via_moto(moto_endpoint: str, tmp_path: Path) -> None:
    """``apply_via_opentofu`` compiles an AWS contract through the plugin
    and provisions Iceberg-on-Glue + the backing S3 bucket through ``tofu``
    against moto. Proves the engine ↔ AWS plugin ↔ tofu wiring works."""
    cid = "iac.engine.live"
    contract = _aws_iceberg_contract("engine-live-bucket", cid=cid)
    plan = _write_plan(tmp_path / "plan.json", contract)
    _drop_moto_provider_sidecar(tmp_path, cid, moto_endpoint)

    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG)

    assert rc == 0
    # Independently confirm — the engine really did the work via tofu.
    table = _boto("glue", moto_endpoint).get_table(DatabaseName="engine_silver", Name="events")[
        "Table"
    ]
    assert table["Parameters"]["table_type"] == "ICEBERG"


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_apply_engine_dry_run_provisions_nothing_via_moto(
    moto_endpoint: str, tmp_path: Path
) -> None:
    """``--dry-run`` stops at ``tofu plan`` — the review point — and creates
    no AWS resources, even when the contract describes them."""
    cid = "iac.engine.dryrun"
    plan = _write_plan(
        tmp_path / "plan.json", _aws_iceberg_contract("engine-dryrun-bucket", cid=cid)
    )
    _drop_moto_provider_sidecar(tmp_path, cid, moto_endpoint)

    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path, dry_run=True), _LOG)

    assert rc == 0
    import botocore.exceptions

    with pytest.raises(botocore.exceptions.ClientError):
        # Database should not exist because nothing was applied.
        _boto("glue", moto_endpoint).get_database(Name="engine_silver")


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_apply_engine_data_loss_gate_blocks_then_allows_via_moto(
    moto_endpoint: str, tmp_path: Path
) -> None:
    """A plan that destroys an AWS resource is blocked closed (``tofu`` has
    no data-snapshot abstraction) and proceeds only with
    ``--allow-data-loss``."""
    cid = "iac.engine.dlgate"
    _drop_moto_provider_sidecar(tmp_path, cid, moto_endpoint)

    # Apply A: S3 + Glue db + Glue table.
    plan_a = _write_plan(
        tmp_path / "a.json", _aws_iceberg_contract("engine-dlgate-bucket", cid=cid)
    )
    assert engine.apply_via_opentofu(_apply_args(plan_a, tmp_path), _LOG) == 0
    # The Glue table really exists.
    _boto("glue", moto_endpoint).get_table(DatabaseName="engine_silver", Name="events")

    # Apply B: same contract id (so it shares A's workdir + state) with the
    # table dropped from the exposure — the plan must remove the table.
    contract_b = {
        "id": cid,
        "name": "drop the table",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        # Keep the database and the bucket — drop the table.
                        "database": "engine_silver",
                        "bucket": "engine-dlgate-bucket",
                        "path": "silver/events/",
                    },
                },
            }
        ],
    }
    plan_b = _write_plan(tmp_path / "b.json", contract_b)

    # The table removal trips the data-loss gate.
    with pytest.raises(CLIError) as excinfo:
        engine.apply_via_opentofu(_apply_args(plan_b, tmp_path), _LOG)
    assert "data" in str(excinfo.value).lower() or "destroy" in str(excinfo.value).lower()
    # The table must survive a blocked apply.
    _boto("glue", moto_endpoint).get_table(DatabaseName="engine_silver", Name="events")

    # --allow-data-loss lets the destructive plan through.
    rc = engine.apply_via_opentofu(_apply_args(plan_b, tmp_path, allow_data_loss=True), _LOG)
    assert rc == 0
    import botocore.exceptions

    with pytest.raises(botocore.exceptions.ClientError):
        _boto("glue", moto_endpoint).get_table(DatabaseName="engine_silver", Name="events")
