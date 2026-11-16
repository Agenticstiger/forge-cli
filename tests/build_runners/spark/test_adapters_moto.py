# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A wire round-trip of the adapters against moto's in-process EMR Serverless and Glue."""

from __future__ import annotations

import dataclasses
import os
import signal
import threading

import boto3
import pytest
from moto import mock_aws

from fluid_build.build_runners.spark import _adapters as ad

pytestmark = pytest.mark.emulated

REGION = "eu-west-1"


def clients(service, region):
    return boto3.client(service, region_name=region)


def test_emr_serverless_runs_and_get_read_back_name_tags_and_arguments(run):
    with mock_aws():
        emr = boto3.client("emr-serverless", region_name=REGION)
        app = emr.create_application(
            name=run.target["applicationName"], releaseLabel="emr-7.12.0", type="SPARK"
        )
        target = dict(applicationId=app["applicationId"])
        # moto raises ResourceNotFound for list_job_runs on an application with no runs, so the
        # job is started with boto3 directly, as the adapter would, before anything is listed.
        started = emr.start_job_run(
            applicationId=app["applicationId"],
            executionRoleArn=run.role_arn,
            name=run.run_name,
            jobDriver={
                "sparkSubmit": {
                    "entryPoint": run.runner_uri,
                    "entryPointArguments": run.entry_args(),
                }
            },
            tags=run.tags(),
        )
        adapter = ad.adapter_for(dataclasses.replace(run, target=target), clients)
        found = adapter.runs()
        assert [r.run_id for r in found] == [started["jobRunId"]]
        got = adapter.get(started["jobRunId"])
        assert (got.config_hash, got.source_id, got.checkpoint_id) == (
            run.config_hash, run.source_id, run.checkpoint_id,
        )  # fmt: skip


def test_emr_serverless_finds_the_application_by_name(run):
    with mock_aws():
        emr = boto3.client("emr-serverless", region_name=REGION)
        app = emr.create_application(
            name=run.target["applicationName"], releaseLabel="emr-7.12.0", type="SPARK"
        )
        assert ad.adapter_for(run, clients).application_id() == app["applicationId"]


def test_glue_start_then_get_job_runs_round_trips_the_arguments(run):
    glue_run = dataclasses.replace(
        run, runtime="glue", target={"glueJobName": "fluid-job"}, release_label=None
    )
    with mock_aws():
        glue = boto3.client("glue", region_name=REGION)
        glue.create_job(
            Name="fluid-job",
            Role=run.role_arn,
            Command={"Name": "glueetl", "ScriptLocation": glue_run.runner_uri},
        )
        adapter = ad.adapter_for(glue_run, clients)
        run_id = adapter.start("tok")
        info = adapter.get(run_id)
        assert info.config_hash == glue_run.config_hash
        assert info.checkpoint_id == glue_run.checkpoint_id
        assert [r.run_id for r in adapter.runs()] == [run_id]


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(), reason="signals need the main thread"
)
def test_sigterm_while_waiting_cancels_the_moto_run_and_reraises(run):
    from moto.emrserverless.models import emrserverless_backends

    from fluid_build.build_runners.spark._reconcile import run_once

    said = []
    with mock_aws():
        emr = boto3.client("emr-serverless", region_name=REGION)
        app_id = emr.create_application(name="app", releaseLabel="emr-7.12.0", type="SPARK")[
            "applicationId"
        ]
        scheduled = dataclasses.replace(
            run, scheduled=True, mode="batch", retry_attempts=None, target={"applicationId": app_id}
        )
        backend = emrserverless_backends["123456789012"][REGION]

        def say(text):
            # moto's job runs start SUCCESS (already terminal): make this one run, as it
            # would be on AWS, once it has been started.
            said.append(text)
            if len(said) == 1:
                backend.job_runs[app_id][-1].state = "RUNNING"

        def sleep(_seconds):
            os.kill(os.getpid(), signal.SIGTERM)

        with pytest.raises(KeyboardInterrupt):
            run_once(scheduled, clients=clients, say=say, sleep=sleep)
        job = backend.job_runs[app_id][-1]
        assert job.state == "CANCELLED"
        assert any(job.id in line for line in said)
