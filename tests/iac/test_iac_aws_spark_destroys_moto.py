# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Spark product's EMR Serverless application destroyed while a run is going, with the
real ``tofu`` against moto (``iac/providers/aws_spark_destroys.py``).

The apply reads the plan's deletes, cancels the run and stops the application, and only then
does ``tofu`` delete it: EMR Serverless (and moto) deletes only a stopped or created
application, so without the hook the destroy failed part way. Skipped unless ``tofu`` is on
PATH and moto's ``server`` extra is installed; ``iac-tests.yml`` Stage 1 installs both and
fails if this file only skipped.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.iac import runner
from fluid_build.iac.naming import safe_ident
from fluid_build.spark import names
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.iac.test_iac_aws_apply_engine_moto import (  # noqa: F401 - moto_endpoint is a fixture
    _apply_args,
    _boto,
    _have_moto_server,
    _write_plan,
    moto_endpoint,
)

pytestmark = [pytest.mark.integration, pytest.mark.provider, pytest.mark.aws]

_LOG = logging.getLogger("test.iac.aws.spark_destroys")
_REGION = "us-east-1"
_SKIP = runner.tofu_path() is None or not _have_moto_server()
_SKIP_REASON = "needs `tofu` on PATH + moto server extra (pip install 'moto[server]')"

CID, BUILD = "shop.orders", "ingest"
APP_KEY = safe_ident(f"{CID}_spark")
SCHEMA = [{"name": "order_id", "type": "string", "required": True}]
CONTRACT = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": CID,
    "name": "Orders",
    "metadata": {"owner": {"team": "shop"}},
    "packaging": {"containers": {"emrApplication": "isolated"}},
    "builds": [
        {
            "id": BUILD,
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["lines"],
            "properties": {
                "stream": {
                    "kind": "kafka",
                    "topic": "shop.orders",
                    "format": "json",
                    "schema": SCHEMA,
                    "trigger": {"processingTime": "PT1M"},
                },
                "mapping": {"columns": [{"name": "order_id", "from": "order_id"}]},
                "parameters": {
                    "spark": {
                        "runtime": "emr-serverless",
                        "source": {"bootstrapServers": "b-1.example.internal:9092", "auth": "none"},
                    }
                },
            },
        }
    ],
    "exposes": [
        {
            "exposeId": "lines",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "shop",
                    "table": "lines",
                    "bucket": "shop-lake",
                    "path": "iceberg/shop/lines/",
                    "region": _REGION,
                },
            },
            "contract": {"schema": SCHEMA},
        }
    ],
}


def _sidecar(workspace: Path, endpoint: str, *, moto_engine_name: bool) -> None:
    """The moto endpoints, merged into the plugin's module (an ``_override`` file)."""
    workdir = workspace / ".fluid" / "iac" / "aws" / safe_ident(CID)
    workdir.mkdir(parents=True, exist_ok=True)
    services = ("s3", "glue", "sts", "iam", "logs", "kms", "emrserverless")
    override = {
        "provider": {
            "aws": {
                "region": _REGION,
                "access_key": "testing",
                "secret_key": "testing",  # pragma: allowlist secret - moto only
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {service: endpoint for service in services},
            }
        }
    }
    if moto_engine_name:
        # moto 5.2 knows the engine only as "SPARK"; AWS takes the "spark" forge emits.
        override["resource"] = {"aws_emrserverless_application": {APP_KEY: {"type": "SPARK"}}}
    (workdir / "provider_override.tf.json").write_text(json.dumps(override), encoding="utf-8")


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_a_destroyed_application_is_emptied_and_stopped_before_tofu_deletes_it(
    moto_endpoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import moto.emrserverless.models as emr_models

    # moto creates an application STARTED and a job run SUCCESS at once; AWS creates the
    # application CREATED (what the provider waits for), and a stream runs until cancelled.
    monkeypatch.setattr(emr_models, "APPLICATION_STATUS", "CREATED", raising=False)
    monkeypatch.setattr(emr_models, "JOB_STATUS", "RUNNING", raising=False)
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("FLUID_SPARK_WAIT_SECONDS", "60")
    plan = tmp_path / "plan.json"

    _sidecar(tmp_path, moto_endpoint, moto_engine_name=True)
    _write_plan(plan, copy.deepcopy(CONTRACT))
    assert engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG) == 0

    emr = _boto("emr-serverless", moto_endpoint)
    (app,) = emr.list_applications()["applications"]
    emr.start_application(applicationId=app["id"])
    job = emr.start_job_run(
        applicationId=app["id"],
        executionRoleArn="arn:aws:iam::123456789012:role/fluid-shop",
        name=names.run_name(CID, BUILD),
        clientToken="fluid-test",
        jobDriver={"sparkSubmit": {"entryPoint": "s3://shop-lake/runner.py"}},
    )["jobRunId"]
    assert emr.get_application(applicationId=app["id"])["application"]["state"] == "STARTED"

    # The build leaves the contract, and with it the product's own application.
    gone = copy.deepcopy(CONTRACT)
    gone["builds"] = []
    del gone["packaging"]
    _sidecar(tmp_path, moto_endpoint, moto_engine_name=False)
    _write_plan(plan, gone)
    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path, allow_data_loss=True), _LOG)

    assert rc == 0
    run = emr.get_job_run(applicationId=app["id"], jobRunId=job)["jobRun"]
    assert run["state"] == "CANCELLED"
    assert emr.get_application(applicationId=app["id"])["application"]["state"] == "TERMINATED"
    # The console wraps its lines at the terminal's width.
    out = " ".join(capsys.readouterr().out.split())
    assert f"cancelled {job} before the apply destroys what it runs with" in out
    assert "stopped the application" in out
