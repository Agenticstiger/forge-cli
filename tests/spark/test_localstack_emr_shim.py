# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The LocalStack lane's EMR Serverless shim, offline: botocore parses a list response that
has no ``id`` (as LocalStack sends it), and the shim fills it before forge's adapter reads it.
Without the shim, the adapter refuses the summary, as it would on AWS."""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

boto3 = pytest.importorskip("boto3")
AWSResponse = pytest.importorskip("botocore.awsrequest").AWSResponse

from fluid_build.build_runners.spark import _adapters  # noqa: E402
from tests.build_runners.spark.conftest import runs_of  # noqa: E402

from ._localstack_emr import with_summary_ids  # noqa: E402
from .conftest import CONTRACT  # noqa: E402

APP_ARN = "arn:aws:emr-serverless:us-east-1:000000000000:/applications/b2f154e9"


class _Raw:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def stream(self, **_kwargs):
        yield self.body


def _localstack_says(client, operation: str, body: dict) -> None:
    """Answer ``operation`` with ``body``, before anything is sent."""

    def answer(request, **_kwargs):
        return AWSResponse(
            request.url, 200, {"Content-Type": "application/json"}, _Raw(json.dumps(body).encode())
        )

    client.meta.events.register(f"before-send.emr-serverless.{operation}", answer)


def _factory(pages, shim):
    def plain(service, region):
        client = boto3.client(
            service, region_name=region, aws_access_key_id="test", aws_secret_access_key="test"
        )
        for operation, body in pages.items():
            _localstack_says(client, operation, body)
        return client

    return with_summary_ids(plain) if shim else plain


@pytest.fixture
def by_name():
    run = runs_of(copy.deepcopy(CONTRACT))[0]
    return dataclasses.replace(run, region="us-east-1", target={"applicationName": "app-x"})


def _app_page(name):
    return {"applications": [{"arn": APP_ARN, "name": name, "state": "STARTED",
                              "releaseLabel": "emr-7.12.0", "type": "Spark",
                              "createdAt": 0, "updatedAt": 0}]}  # fmt: skip


def test_the_shim_fills_the_application_id_localstack_leaves_out(by_name):
    clients = _factory({"ListApplications": _app_page("app-x")}, shim=True)
    assert _adapters.adapter_for(by_name, clients).application_id() == "b2f154e9"


def test_without_the_shim_the_adapter_refuses_the_summary(by_name):
    clients = _factory({"ListApplications": _app_page("app-x")}, shim=False)
    with pytest.raises(_adapters.SparkRunError, match="application summary carries no id"):
        _adapters.adapter_for(by_name, clients).application_id()


def test_the_shim_fills_job_run_ids_through_the_paginator(by_name):
    run = dataclasses.replace(by_name, target={"applicationId": "b2f154e9"})
    summary = {
        "arn": f"{APP_ARN}/jobruns/ff81f100", "applicationId": "b2f154e9", "name": run.run_name,
        "state": "RUNNING", "stateDetails": "", "releaseLabel": "emr-7.12.0",
        "createdBy": "x", "createdAt": 0, "updatedAt": 0,
    }  # fmt: skip
    clients = _factory({"ListJobRuns": {"jobRuns": [summary]}}, shim=True)
    adapter = _adapters.adapter_for(run, clients)
    seen = []
    adapter.get = lambda run_id: seen.append(run_id)  # the read after the list is not the shim's
    adapter.runs()
    assert "ff81f100" in seen
