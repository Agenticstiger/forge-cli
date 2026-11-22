# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The four runtime adapters against botocore's Stubber, which checks every request and
response against the real AWS API model."""

from __future__ import annotations

import dataclasses
import datetime as dt
import io

import pytest

# boto3 is the [aws] extra: a lane without it skips the request-shape tests.
boto3 = pytest.importorskip("boto3")
Stubber = pytest.importorskip("botocore.stub").Stubber

from fluid_build.build_runners.spark import _adapters as ad
from fluid_build.build_runners.spark._desired import config_digest
from fluid_build.spark import runner_source
from fluid_build.spark.names import RUNNER_FILE
from tests.build_runners.spark.conftest import variant

NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)
EARLIER = NOW - dt.timedelta(hours=1)
ARN = "arn:aws:emr-serverless:eu-west-1:123456789012:/applications/app1/jobruns/xxxxxxxxxxxx"
USER = "arn:aws:iam::123456789012:user/fluid"


def stubbed(service, region="eu-west-1"):
    client = boto3.client(service, region_name=region)
    stub = Stubber(client)
    stub.activate()
    return client, stub


class ConfigBucket:
    """The S3 the artifacts are read back from before a start: the job config (``body``)
    and the runner (``runner``)."""

    def __init__(self, body, runner=None):
        self.body, self.runner, self.read = body, runner, []

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        self.read.append(f"s3://{Bucket}/{Key}")
        if Key.endswith("/failed"):  # no config has failed its probation
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "no"}}, "GetObject")
        if Key.endswith(RUNNER_FILE):
            return {"Body": io.BytesIO(self.runner or runner_source().encode("utf-8"))}
        return {"Body": io.BytesIO(self.body)}


def adapter_on(run, service, body=None, runner=None):
    client, stub = stubbed(service, run.region)
    bucket = ConfigBucket(run.job_text.encode("utf-8") if body is None else body, runner)
    return ad.adapter_for(run, lambda s, r: bucket if s == "s3" else client), stub


def started(run):
    """``run`` as an adapter starts it: with the digest of the job config it read back."""
    return dataclasses.replace(run, job_sha256=config_digest(run.job_text))


def identity_of(run):
    return {
        "config_hash": run.config_hash,
        "source_id": run.source_id,
        "checkpoint_id": run.checkpoint_id,
        "config_uri": run.job_uri,
        **({"job_sha256": run.job_sha256} if run.job_sha256 else {}),
    }


@pytest.fixture
def serverless(run):
    return dataclasses.replace(run, target={"applicationId": "app1"})


# --- EMR Serverless -------------------------------------------------------------------


def _serverless_start(run):
    request = {
        "applicationId": "app1",
        "clientToken": "tok",
        "executionRoleArn": run.role_arn,
        "name": run.run_name,
        "mode": "STREAMING",
        "jobDriver": {
            "sparkSubmit": {
                "entryPoint": run.runner_uri,
                "entryPointArguments": started(run).entry_args(stop_key="tok"),
                "sparkSubmitParameters": ad._submit_parameters(run.spark_conf),
            }
        },
        "tags": run.tags(),
    }  # fmt: skip
    return request


def test_serverless_streaming_start(serverless):
    adapter, stub = adapter_on(serverless, "emr-serverless")
    request = _serverless_start(serverless)
    request["retryPolicy"] = {"maxFailedAttemptsPerHour": 5}
    stub.add_response(
        "start_job_run", {"applicationId": "app1", "jobRunId": "jr1", "arn": ARN}, request
    )
    assert adapter.start("tok") == "jr1"
    stub.assert_no_pending_responses()
    args = request["jobDriver"]["sparkSubmit"]["entryPointArguments"]
    assert args[:4] == [
        "--config",
        serverless.job_uri,
        "--fluid-config-hash",
        serverless.config_hash,
    ]
    assert request["tags"]["fluid_build"] == "ingest-orders"


def test_serverless_scheduled_start_is_batch_with_max_attempts(serverless):
    scheduled = dataclasses.replace(serverless, scheduled=True, mode="batch", retry_attempts=2)
    adapter, stub = adapter_on(scheduled, "emr-serverless")
    request = _serverless_start(scheduled)
    request.update(mode="BATCH", retryPolicy={"maxAttempts": 2})
    stub.add_response(
        "start_job_run", {"applicationId": "app1", "jobRunId": "jr2", "arn": ARN}, request
    )
    assert adapter.start("tok") == "jr2"


def _job_run(run, state, run_id="jr1", *, created=NOW, attempt=None, details="ok", with_args=True):
    driver = {"entryPoint": run.runner_uri}
    if with_args:
        driver["entryPointArguments"] = run.entry_args()
    job = {
        "applicationId": "app1", "jobRunId": run_id, "arn": ARN, "createdBy": USER,
        "createdAt": created, "updatedAt": created, "executionRole": run.role_arn,
        "state": state, "stateDetails": details, "releaseLabel": "emr-7.12.0",
        "jobDriver": {"sparkSubmit": driver}, "tags": run.tags(),
    }  # fmt: skip
    if attempt:
        job["attempt"] = attempt
    return {"jobRun": job}


@pytest.mark.parametrize(
    ("raw", "state"),
    [("SUCCESS", "SUCCEEDED"), ("SCHEDULED", "PENDING"), ("CANCELLING", "CANCELLING"),
     ("FAILED", "FAILED"), ("RUNNING", "RUNNING")],
)  # fmt: skip
def test_serverless_get_normalises_state_and_reads_identity(serverless, raw, state):
    adapter, stub = adapter_on(serverless, "emr-serverless")
    stub.add_response(
        "get_job_run",
        _job_run(serverless, raw, attempt=2, details="boom"),
        {"applicationId": "app1", "jobRunId": "jr1"},
    )
    info = adapter.get("jr1")
    assert (info.state, info.raw_state, info.attempt, info.details) == (state, raw, 2, "boom")
    assert {k: getattr(info, k) for k in identity_of(serverless)} == identity_of(serverless)


def test_serverless_runs_lists_this_runs_name_newest_first(serverless):
    adapter, stub = adapter_on(serverless, "emr-serverless")

    def summary(run_id, name, created):
        return {
            "applicationId": "app1", "id": run_id, "arn": ARN, "createdBy": USER,
            "createdAt": created, "updatedAt": created, "executionRole": USER, "state": "RUNNING",
            "name": name, "type": "SPARK", "stateDetails": "running", "releaseLabel": "emr-7.12.0",
        }  # fmt: skip

    active = ["CANCELLING", "PENDING", "QUEUED", "RUNNING", "SCHEDULED", "SUBMITTED"]
    # Active runs first, every page and filtered by state; then the recent ones, unfiltered.
    stub.add_response(
        "list_job_runs",
        {"jobRuns": [summary("new", serverless.run_name, NOW)], "nextToken": "t"},
        {"applicationId": "app1", "states": active},
    )
    stub.add_response(
        "list_job_runs",
        {"jobRuns": [summary("other", "someone-else", NOW)]},
        {"applicationId": "app1", "states": active, "nextToken": "t"},
    )
    stub.add_response(
        "list_job_runs",
        {"jobRuns": [
            summary("old", serverless.run_name, EARLIER),
            summary("new", serverless.run_name, NOW),
        ]},
        {"applicationId": "app1"},
    )  # fmt: skip
    for run_id in ("new", "old"):
        stub.add_response(
            "get_job_run",
            _job_run(serverless, "RUNNING", run_id),
            {"applicationId": "app1", "jobRunId": run_id},
        )
    assert [r.run_id for r in adapter.runs()] == ["new", "old"]


def test_serverless_cancel(serverless):
    adapter, stub = adapter_on(serverless, "emr-serverless")
    stub.add_response(
        "cancel_job_run",
        {"applicationId": "app1", "jobRunId": "jr1"},
        {"applicationId": "app1", "jobRunId": "jr1"},
    )
    adapter.cancel("jr1")
    stub.assert_no_pending_responses()


def _applications(*apps):
    return {
        "applications": [
            {
                "id": i,
                "name": n,
                "arn": ARN,
                "releaseLabel": "emr-7.12.0",
                "type": "Spark",
                "state": s,
                "createdAt": NOW,
                "updatedAt": NOW,
            }  # fmt: skip
            for i, n, s in apps
        ]
    }


def test_serverless_resolves_the_application_by_name(run):
    adapter, stub = adapter_on(run, "emr-serverless")
    name = run.target["applicationName"]
    stub.add_response(
        "list_applications",
        _applications(
            ("gone", name, "TERMINATED"), ("app9", name, "STARTED"), ("x", "other", "STARTED")
        ),
    )
    assert adapter.application_id() == "app9"
    assert adapter.application_id() == "app9"  # cached: no second call
    stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("apps", "message"),
    [([], "no EMR Serverless application named"),
     ([("a", "N", "STARTED"), ("b", "N", "CREATED")], "2 EMR Serverless applications")],
)  # fmt: skip
def test_serverless_application_lookup_needs_exactly_one_match(run, apps, message):
    adapter, stub = adapter_on(run, "emr-serverless")
    apps = [(i, run.target["applicationName"], s) for i, _, s in apps]
    stub.add_response("list_applications", _applications(*apps))
    with pytest.raises(ad.SparkRunError, match=message):
        adapter.application_id()


# --- Glue -----------------------------------------------------------------------------


@pytest.fixture
def glue_run(run):
    return variant(
        run, "glue", {"glueJobName": "fluid-job"}, release_label=None, glue_version="5.0"
    )


def test_glue_start_passes_identity_and_conf_as_arguments(glue_run):
    adapter, stub = adapter_on(glue_run, "glue")
    conf = sorted(glue_run.spark_conf.items())
    arguments = {
        "--config": glue_run.job_uri,
        "--fluid-config-hash": glue_run.config_hash,
        "--fluid-source": glue_run.source_id,
        "--fluid-checkpoint": glue_run.checkpoint_id,
        "--fluid-job-sha256": started(glue_run).job_sha256,
        "--fluid-stop-key": "tok",
        "--conf": " --conf ".join(f"{k}={v}" for k, v in conf),
    }
    stub.add_response(
        "start_job_run", {"JobRunId": "jr_1"}, {"JobName": "fluid-job", "Arguments": arguments}
    )
    assert adapter.start("tok") == "jr_1"


def test_glue_runs_normalise_states_and_read_arguments(glue_run):
    adapter, stub = adapter_on(glue_run, "glue")
    flat = dict(zip(glue_run.entry_args()[::2], glue_run.entry_args()[1::2], strict=True))
    stub.add_response(
        "get_job_runs",
        {"JobRuns": [
            {"Id": "jr_old", "JobRunState": "STOPPED", "StartedOn": EARLIER, "Arguments": flat},
            {"Id": "jr_new", "JobRunState": "TIMEOUT", "StartedOn": NOW, "Arguments": flat,
             "ErrorMessage": "timed out", "Attempt": 1},
        ]},
        {"JobName": "fluid-job", "MaxResults": 25},
    )  # fmt: skip
    new, old = adapter.runs()
    assert (new.run_id, new.state, new.details) == ("jr_new", "FAILED", "timed out")
    assert (old.run_id, old.state) == ("jr_old", "CANCELLED")
    assert old.config_hash == glue_run.config_hash and old.checkpoint_id == glue_run.checkpoint_id


def test_glue_get(glue_run):
    adapter, stub = adapter_on(glue_run, "glue")
    stub.add_response(
        "get_job_run",
        {"JobRun": {"Id": "jr_1", "JobRunState": "SUCCEEDED"}},
        {"JobName": "fluid-job", "RunId": "jr_1"},
    )
    assert adapter.get("jr_1").state == "SUCCEEDED"


def test_glue_cancel_reports_batch_stop_errors(glue_run):
    adapter, stub = adapter_on(glue_run, "glue")
    expected = {"JobName": "fluid-job", "JobRunIds": ["jr_1"]}
    stub.add_response("batch_stop_job_run", {"SuccessfulSubmissions": [], "Errors": []}, expected)
    adapter.cancel("jr_1")
    error = {"JobRunId": "jr_1", "ErrorDetail": {"ErrorCode": "X", "ErrorMessage": "not running"}}
    stub.add_response(
        "batch_stop_job_run", {"Errors": [{"JobName": "fluid-job", **error}]}, expected
    )
    with pytest.raises(ad.SparkRunError, match="not running"):
        adapter.cancel("jr_1")


# --- EMR on EC2 -----------------------------------------------------------------------


@pytest.fixture
def ec2_run(run):
    return variant(run, "emr-ec2", {"clusterId": "j-ABC"})


def test_ec2_start_adds_a_spark_submit_step_that_continues_on_failure(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    args = ["spark-submit", "--deploy-mode", "client", *ad._conf_params(ec2_run.spark_conf)]
    args += [ec2_run.runner_uri, *started(ec2_run).entry_args(stop_key="tok")]
    stub.add_response(
        "add_job_flow_steps",
        {"StepIds": ["s-1"]},
        {"JobFlowId": "j-ABC", "Steps": [{
            "Name": ec2_run.run_name,
            "ActionOnFailure": "CONTINUE",
            "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args},
        }]},
    )  # fmt: skip
    assert adapter.start("tok") == "s-1"
    assert "--fluid-config-hash" in args and ec2_run.config_hash in args


def _step(run, step_id, state, created, name=None, reason="why"):
    return {
        "Id": step_id,
        "Name": name or run.run_name,
        "Config": {"Jar": "command-runner.jar", "Args": run.entry_args()},
        "ActionOnFailure": "CONTINUE",
        "Status": {
            "State": state,
            "StateChangeReason": {"Message": reason},
            "Timeline": {"CreationDateTime": created},
        },
    }


def test_ec2_runs_filter_by_name_and_map_interrupted_to_failed(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    # Every page of active steps first: a newer step can push a running one off page one.
    stub.add_response(
        "list_steps",
        {"Steps": [_step(ec2_run, "s-other", "RUNNING", NOW, name="unrelated")], "Marker": "m"},
        {"ClusterId": "j-ABC", "StepStates": ["PENDING", "RUNNING", "CANCEL_PENDING"]},
    )
    stub.add_response(
        "list_steps",
        {"Steps": []},
        {
            "ClusterId": "j-ABC",
            "StepStates": ["PENDING", "RUNNING", "CANCEL_PENDING"],
            "Marker": "m",
        },
    )
    stub.add_response(
        "list_steps",
        {"Steps": [
            _step(ec2_run, "s-old", "COMPLETED", EARLIER),
            _step(ec2_run, "s-new", "INTERRUPTED", NOW, reason="cluster stopped"),
        ]},
        {"ClusterId": "j-ABC"},
    )  # fmt: skip
    new, old = adapter.runs()
    assert (new.run_id, new.state, new.raw_state, new.details) == (
        "s-new", "FAILED", "INTERRUPTED", "cluster stopped",
    )  # fmt: skip
    assert old.state == "SUCCEEDED" and old.config_hash == ec2_run.config_hash


def test_ec2_get_reads_describe_step(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    stub.add_response(
        "describe_step",
        {"Step": _step(ec2_run, "s-1", "CANCEL_PENDING", NOW)},
        {"ClusterId": "j-ABC", "StepId": "s-1"},
    )
    assert adapter.get("s-1").state == "CANCELLING"


def test_ec2_cancel_failure_raises(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    expected = {
        "ClusterId": "j-ABC",
        "StepIds": ["s-1"],
        "StepCancellationOption": "SEND_INTERRUPT",
    }
    stub.add_response(
        "cancel_steps",
        {"CancelStepsInfoList": [{"StepId": "s-1", "Status": "SUBMITTED"}]},
        expected,
    )
    adapter.cancel("s-1")
    stub.add_response(
        "cancel_steps",
        {"CancelStepsInfoList": [{"StepId": "s-1", "Status": "FAILED", "Reason": "already done"}]},
        expected,
    )
    with pytest.raises(ad.SparkRunError, match="already done"):
        adapter.cancel("s-1")


# --- EMR on EKS -----------------------------------------------------------------------


@pytest.fixture
def eks_run(run):
    return variant(run, "emr-eks", {"virtualClusterId": "vc1"}, retry_attempts=3)


def _eks_job(run, run_id, state, created=NOW, **extra):
    driver = {"entryPoint": run.runner_uri, "entryPointArguments": run.entry_args()}
    return {
        "id": run_id, "name": run.run_name, "virtualClusterId": "vc1", "state": state,
        "createdAt": created, "jobDriver": {"sparkSubmitJobDriver": driver},
        "tags": run.tags(), **extra,
    }  # fmt: skip


def test_eks_start_sets_cluster_release_and_retry_policy(eks_run):
    adapter, stub = adapter_on(eks_run, "emr-containers")
    stub.add_response(
        "start_job_run",
        {"id": "eks-1", "name": eks_run.run_name, "virtualClusterId": "vc1"},
        {
            "name": eks_run.run_name,
            "virtualClusterId": "vc1",
            "clientToken": "tok",
            "executionRoleArn": eks_run.role_arn,
            "releaseLabel": "emr-7.12.0",
            "jobDriver": {"sparkSubmitJobDriver": {
                "entryPoint": eks_run.runner_uri,
                "entryPointArguments": started(eks_run).entry_args(stop_key="tok"),
                "sparkSubmitParameters": ad._submit_parameters(eks_run.spark_conf),
            }},
            "tags": eks_run.tags(),
            "retryPolicyConfiguration": {"maxAttempts": 3},
        },
    )  # fmt: skip
    assert adapter.start("tok") == "eks-1"


def test_eks_runs_and_get_normalise_completed_and_read_attempts(eks_run):
    adapter, stub = adapter_on(eks_run, "emr-containers")
    stub.add_response(
        "list_job_runs",
        {"jobRuns": [
            _eks_job(eks_run, "e-old", "COMPLETED", EARLIER),
            _eks_job(eks_run, "e-new", "RUNNING", retryPolicyExecution={"currentAttemptCount": 3}),
        ]},
        {"virtualClusterId": "vc1", "name": eks_run.run_name},
    )  # fmt: skip
    new, old = adapter.runs()
    assert (new.run_id, new.state, new.attempt) == ("e-new", "RUNNING", 3)
    assert (old.state, old.config_hash) == ("SUCCEEDED", eks_run.config_hash)
    stub.add_response(
        "describe_job_run",
        {"jobRun": _eks_job(eks_run, "e-old", "FAILED", failureReason="USER_ERROR")},
        {"id": "e-old", "virtualClusterId": "vc1"},
    )
    got = adapter.get("e-old")
    assert (got.state, got.details) == ("FAILED", "USER_ERROR")


def test_eks_cancel(eks_run):
    adapter, stub = adapter_on(eks_run, "emr-containers")
    stub.add_response(
        "cancel_job_run",
        {"id": "e-1", "virtualClusterId": "vc1"},
        {"id": "e-1", "virtualClusterId": "vc1"},
    )
    adapter.cancel("e-1")
    stub.assert_no_pending_responses()


def test_where_names_the_target(serverless, glue_run, ec2_run, eks_run):
    assert "j-ABC" in adapter_on(ec2_run, "emr")[0].where()
    assert "fluid-job" in adapter_on(glue_run, "glue")[0].where()
    assert "vc1" in adapter_on(eks_run, "emr-containers")[0].where()
    assert "app1" in adapter_on(serverless, "emr-serverless")[0].where()


def test_a_serverless_summary_is_read_by_its_id():
    assert ad._required_id({"id": "abc", "arn": ARN}, "job run summary") == "abc"
    assert ad._required_id({"jobRunId": "jr", "id": "x"}, "job run", "jobRunId") == "jr"


@pytest.mark.parametrize("summary", [{"arn": ARN, "name": "x"}, {"id": ""}, {}])
def test_a_serverless_summary_without_an_id_is_a_clear_error(summary):
    """AWS always returns ``id``; an emulator that does not is the test's to shim."""
    with pytest.raises(ad.SparkRunError, match="job run summary carries no id; its keys: ") as err:
        ad._required_id(summary, "job run summary")
    for key in summary:
        assert key in str(err.value)
    assert "xxxxxxxxxxxx" not in str(err.value)  # the ARN is never parsed for one


def test_a_serverless_job_run_without_a_job_run_id_is_a_clear_error(serverless):
    """GetJobRun's jobRun carries jobRunId on AWS; an ``id`` alone is not read as one."""
    adapter, stub = adapter_on(serverless, "emr-serverless")
    adapter.client.get_job_run = lambda **_k: {"jobRun": {"id": "jr1", "state": "RUNNING"}}
    with pytest.raises(ad.SparkRunError, match="job run carries no jobRunId; its keys: id, state"):
        adapter.get("jr1")


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **_kwargs):
        return iter(self.pages)


@pytest.mark.parametrize(
    "operation, call",
    [
        ("list_applications", lambda a: a.application_id()),
        ("list_job_runs", lambda a: a.runs()),
    ],
)
def test_the_adapter_refuses_a_list_summary_without_an_id(run, serverless, operation, call):
    target = run if operation == "list_applications" else serverless
    adapter, _stub = adapter_on(target, "emr-serverless")
    key = "applications" if operation == "list_applications" else "jobRuns"
    summary = {"arn": ARN, "name": target.target.get("applicationName") or target.run_name}
    adapter.client.get_paginator = lambda name: _Pages([{key: [summary]}])
    what = "application" if operation == "list_applications" else "job run"
    with pytest.raises(
        ad.SparkRunError, match=f"{what} summary carries no id; its keys: arn, name"
    ):
        call(adapter)


def test_a_boto3_without_streaming_job_runs_is_refused(serverless, monkeypatch):
    client = boto3.client("emr-serverless", region_name="eu-west-1")
    shape = client.meta.service_model.operation_model("StartJobRun").input_shape
    members = {k: v for k, v in shape.members.items() if k != "mode"}
    monkeypatch.setattr(type(shape), "members", property(lambda self: members))
    with pytest.raises(ad.SparkRunError, match="upgrade"):
        ad.adapter_for(serverless, lambda service, region: client)


def test_adapter_for_binds_the_account_from_the_credentials(serverless, monkeypatch):
    import dataclasses

    from fluid_build.build_runners.spark import _desired

    token_run = dataclasses.replace(
        serverless, role_arn=f"arn:aws:iam::{_desired.ACCOUNT_TOKEN}:role/r"
    )
    monkeypatch.setattr(_desired, "resolve_account", lambda region: "210987654321")
    client = boto3.client("emr-serverless", region_name="eu-west-1")
    adapter = ad.adapter_for(token_run, lambda service, region: client)
    assert adapter.run.role_arn == "arn:aws:iam::210987654321:role/r"


def test_ec2_history_pages_until_this_builds_last_step(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    active = {"ClusterId": "j-ABC", "StepStates": ["PENDING", "RUNNING", "CANCEL_PENDING"]}
    stub.add_response("list_steps", {"Steps": []}, active)
    others = [_step(ec2_run, f"s-{i}", "COMPLETED", NOW, name="unrelated") for i in range(3)]
    stub.add_response("list_steps", {"Steps": others, "Marker": "m"}, {"ClusterId": "j-ABC"})
    stub.add_response(
        "list_steps",
        {"Steps": [_step(ec2_run, "s-mine", "FAILED", EARLIER)], "Marker": "m2"},
        {"ClusterId": "j-ABC", "Marker": "m"},
    )
    assert [(r.run_id, r.state) for r in adapter.runs()] == [("s-mine", "FAILED")]
    stub.assert_no_pending_responses()


# --- restarts, the restart window, the cluster's state, the uploaded job config ---------


def test_a_restart_carries_the_run_it_restarts_and_reads_back_as_one(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    args = ["spark-submit", "--deploy-mode", "client", *ad._conf_params(ec2_run.spark_conf)]
    args += [ec2_run.runner_uri, *started(ec2_run).entry_args("s-dead", stop_key="tok")]
    assert args[-6:] == [
        "--fluid-job-sha256",
        started(ec2_run).job_sha256,
        "--fluid-stop-key",
        "tok",
        "--fluid-restart-of",
        "s-dead",
    ]
    stub.add_response(
        "add_job_flow_steps",
        {"StepIds": ["s-2"]},
        {"JobFlowId": "j-ABC", "Steps": [{
            "Name": ec2_run.run_name,
            "ActionOnFailure": "CONTINUE",
            "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args},
        }]},
    )  # fmt: skip
    assert adapter.start("tok", restart_of="s-dead") == "s-2"
    step = _step(ec2_run, "s-2", "RUNNING", NOW)
    step["Config"]["Args"] = args
    stub.add_response("describe_step", {"Step": step}, {"ClusterId": "j-ABC", "StepId": "s-2"})
    restarted = adapter.get("s-2")
    assert (restarted.restart_of, restarted.config_hash) == ("s-dead", ec2_run.config_hash)
    assert restarted.stop_key == "tok"  # what a later release asks it to stop by


def test_serverless_restart_marks_its_arguments(serverless):
    adapter, stub = adapter_on(serverless, "emr-serverless")
    request = _serverless_start(serverless)
    request["jobDriver"]["sparkSubmit"]["entryPointArguments"] += ["--fluid-restart-of", "jr0"]
    request["retryPolicy"] = {"maxFailedAttemptsPerHour": 5}
    stub.add_response(
        "start_job_run", {"applicationId": "app1", "jobRunId": "jr1", "arn": ARN}, request
    )
    assert adapter.start("tok", restart_of="jr0") == "jr1"


def test_ec2_history_pages_back_to_the_restart_window(ec2_run):
    """This build's last step is on page one, but a budget counts its steps of the last
    hour: ListSteps lists newest first, so it pages until a step is older than ``since``."""
    adapter, stub = adapter_on(ec2_run, "emr")
    active = {"ClusterId": "j-ABC", "StepStates": ["PENDING", "RUNNING", "CANCEL_PENDING"]}
    stub.add_response("list_steps", {"Steps": []}, active)
    stub.add_response(
        "list_steps",
        {"Steps": [_step(ec2_run, "s-3", "FAILED", NOW)], "Marker": "m"},
        {"ClusterId": "j-ABC"},
    )
    stub.add_response(
        "list_steps",
        {"Steps": [
            _step(ec2_run, "s-2", "FAILED", NOW - dt.timedelta(minutes=30)),
            _step(ec2_run, "s-1", "FAILED", NOW - dt.timedelta(minutes=90)),
        ], "Marker": "m2"},
        {"ClusterId": "j-ABC", "Marker": "m"},
    )  # fmt: skip
    since = (NOW - dt.timedelta(hours=1)).timestamp()
    assert [r.run_id for r in adapter.runs(since=since)] == ["s-3", "s-2", "s-1"]
    stub.assert_no_pending_responses()  # not the page after the one that reached ``since``


def _cluster(state, code=None, message=None):
    reason = {k: v for k, v in (("Code", code), ("Message", message)) if v}
    return {"Cluster": {"Id": "j-ABC", "Status": {"State": state, "StateChangeReason": reason}}}


@pytest.mark.parametrize("state", ["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"])
def test_a_cluster_that_takes_steps_passes_the_check(ec2_run, state):
    adapter, stub = adapter_on(ec2_run, "emr")
    stub.add_response("describe_cluster", _cluster(state), {"ClusterId": "j-ABC"})
    adapter.check_target()


def test_a_cluster_that_ended_is_refused_with_its_reason(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    stub.add_response(
        "describe_cluster",
        _cluster("TERMINATED_WITH_ERRORS", "INSTANCE_FAILURE", "Primary node lost"),
        {"ClusterId": "j-ABC"},
    )
    with pytest.raises(ad.SparkRunError) as raised:
        adapter.check_target()
    assert "j-ABC is TERMINATED_WITH_ERRORS (INSTANCE_FAILURE: Primary node lost)" in str(
        raised.value
    )
    assert "Point parameters.spark.clusterId at a running cluster" in str(raised.value)


def test_a_cluster_this_identity_cannot_read_is_left_to_the_start(ec2_run):
    adapter, stub = adapter_on(ec2_run, "emr")
    stub.add_client_error("describe_cluster", "AccessDeniedException", "not allowed")
    adapter.check_target()  # AddJobFlowSteps reports what EMR says


def test_the_uploaded_job_config_is_read_back(ec2_run):
    s3, stub = stubbed("s3", ec2_run.region)
    emr = boto3.client("emr", region_name=ec2_run.region)
    adapter = ad.adapter_for(ec2_run, lambda service, region: s3 if service == "s3" else emr)
    bucket, _, key = ec2_run.job_uri[len("s3://") :].partition("/")
    import io

    from botocore.response import StreamingBody

    body = b'{"mode": "streaming"}\n'
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(body), len(body))},
        {"Bucket": bucket, "Key": key},
    )
    assert adapter.deployed_job() == body
    stub.add_client_error("get_object", "NoSuchKey", "gone", 404)
    assert adapter.deployed_job() is None
    stub.add_client_error("get_object", "AccessDenied", "no", 403)
    with pytest.raises(ad.SparkRunError, match="needs s3:GetObject on it"):
        adapter.deployed_job()
    huge = b" " * (ad.MAX_ARTIFACT_BYTES + 10)
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(huge), len(huge))},
        {"Bucket": bucket, "Key": key},
    )
    with pytest.raises(ad.SparkRunError, match="more than forge ever uploads"):
        adapter.deployed_job()


def test_serverless_history_keeps_every_run_back_to_the_restart_window(serverless):
    """Past the last five ended runs, the ones of the last hour still come back: a restart
    budget of up to 10 counts them all."""
    adapter, stub = adapter_on(serverless, "emr-serverless")

    def summary(run_id, created):
        return {
            "applicationId": "app1", "id": run_id, "arn": ARN, "createdBy": USER,
            "createdAt": created, "updatedAt": created, "executionRole": USER,
            "state": "SUCCESS", "name": serverless.run_name, "type": "SPARK",
            "stateDetails": "done", "releaseLabel": "emr-7.12.0",
        }  # fmt: skip

    active = ["CANCELLING", "PENDING", "QUEUED", "RUNNING", "SCHEDULED", "SUBMITTED"]
    stub.add_response("list_job_runs", {"jobRuns": []}, {"applicationId": "app1", "states": active})
    recent = [(f"jr{i}", NOW - dt.timedelta(minutes=5 * i)) for i in range(8)]  # 0-35 min old
    old = [(f"jr-old{i}", NOW - dt.timedelta(hours=3, minutes=i)) for i in range(3)]
    stub.add_response(
        "list_job_runs",
        {"jobRuns": [summary(r, c) for r, c in recent + old]},
        {"applicationId": "app1"},
    )
    for run_id, created in recent:
        stub.add_response(
            "get_job_run",
            _job_run(serverless, "SUCCESS", run_id, created=created),
            {"applicationId": "app1", "jobRunId": run_id},
        )
    since = (NOW - dt.timedelta(hours=1)).timestamp()
    assert [r.run_id for r in adapter.runs(since=since)] == [r for r, _ in recent]
    stub.assert_no_pending_responses()


def test_ec2_history_of_a_going_stream_stops_at_the_restart_window(ec2_run):
    """A build with a step going needs no older end than the window: one page, not ten."""
    adapter, stub = adapter_on(ec2_run, "emr")
    active = {"ClusterId": "j-ABC", "StepStates": ["PENDING", "RUNNING", "CANCEL_PENDING"]}
    stub.add_response("list_steps", {"Steps": [_step(ec2_run, "s-9", "RUNNING", NOW)]}, active)
    others = [
        _step(ec2_run, "s-x", "COMPLETED", NOW - dt.timedelta(hours=2), name="unrelated"),
    ]
    stub.add_response("list_steps", {"Steps": others, "Marker": "m"}, {"ClusterId": "j-ABC"})
    since = (NOW - dt.timedelta(hours=1)).timestamp()
    assert [r.run_id for r in adapter.runs(since=since)] == ["s-9"]
    stub.assert_no_pending_responses()


def test_an_unreadable_job_config_says_what_access_denied_can_also_mean(ec2_run):
    s3, stub = stubbed("s3", ec2_run.region)
    emr = boto3.client("emr", region_name=ec2_run.region)
    adapter = ad.adapter_for(ec2_run, lambda service, region: s3 if service == "s3" else emr)
    stub.add_client_error("get_object", "AccessDenied", "no", 403)
    with pytest.raises(ad.SparkRunError, match="without s3:ListBucket on the bucket"):
        adapter.deployed_job()


# --- the job config, read back before a start -------------------------------------------


def test_the_arguments_carry_the_config_s_identity_then_its_sha256(serverless):
    args = started(serverless).entry_args()
    assert args[:2] == ["--config", serverless.job_uri]
    assert args[-2:] == ["--fluid-job-sha256", started(serverless).job_sha256]
    # The identity a run is read back by stays where it was.
    assert ad._identity(args) == identity_of(started(serverless))


@pytest.mark.parametrize(
    "body",
    [b"{}", b"not json", "{\u00e9".encode("latin-1")],
    ids=["another config", "not json", "not utf-8"],
)
def test_a_config_this_apply_did_not_compile_starts_nothing(serverless, body):
    adapter, stub = adapter_on(serverless, "emr-serverless", body=body)
    with pytest.raises(ad.SparkRunError, match="is not the one this apply compiled"):
        adapter.start("tok")
    stub.assert_no_pending_responses()  # nothing was sent to EMR


def test_the_check_before_a_cancel_and_the_start_read_the_artifacts_once(serverless):
    adapter, _ = adapter_on(serverless, "emr-serverless")
    bucket = adapter.clients("s3", serverless.region)
    assert adapter.checked() is adapter.checked()
    assert bucket.read == [serverless.job_uri, serverless.runner_uri]


def test_a_runner_this_forge_does_not_ship_starts_nothing(serverless):
    adapter, stub = adapter_on(serverless, "emr-serverless", runner=b"import os  # not ours\n")
    with pytest.raises(ad.SparkRunError, match="or its runner is not the one"):
        adapter.start("tok")
    stub.assert_no_pending_responses()


def test_an_artifact_larger_than_forge_writes_starts_nothing(serverless, monkeypatch):
    monkeypatch.setattr(ad, "MAX_ARTIFACT_BYTES", 8)
    adapter, _ = adapter_on(serverless, "emr-serverless")
    with pytest.raises(ad.SparkRunError, match="larger than any artifact forge writes"):
        adapter.start("tok")


def test_a_config_that_cannot_be_read_starts_nothing(serverless):
    class Denied:
        def get_object(self, **_):
            raise RuntimeError("AccessDenied")

    client, _ = stubbed("emr-serverless")
    adapter = ad.adapter_for(serverless, lambda s, r: Denied() if s == "s3" else client)
    with pytest.raises(ad.SparkRunError, match="cannot be read to check it"):
        adapter.start("tok")
