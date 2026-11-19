# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A plan that takes a Spark build away on AWS (``iac/providers/aws_spark_destroys.py``):
the build's runs are stopped before ``tofu apply`` destroys what they run with, and a
policy someone attached refuses the apply first. Every AWS call goes through botocore's
Stubber, which checks it against the real API model."""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import aws_spark_destroys as destroys
from fluid_build.spark import names
from tests.iac.test_iac_aws_spark_infra import (  # noqa: F401 - operator_policy is a fixture
    CONTRACT,
    KEY,
    VIRTUAL_CLUSTER,
    _runtime,
    emit,
    operator_policy,
)

boto3 = pytest.importorskip("boto3")
Stubber = pytest.importorskip("botocore.stub").Stubber

pytestmark = [pytest.mark.unit, pytest.mark.provider]

PRODUCT, BUILD = "commerce.orders-stream", "ingest-orders"
RUN_NAME = names.run_name(PRODUCT, BUILD)
REGION = "eu-west-1"
APP = "00fabc123def"  # pragma: allowlist secret - an id
POLICY_ARN = f"arn:aws:iam::000000000005:policy/{names.execution_role_name(PRODUCT, BUILD)}"
NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)
USER = "arn:aws:iam::000000000005:user/fluid"
JOB_ARN = f"arn:aws:emr-serverless:{REGION}:000000000005:/applications/{APP}/jobruns/jr1"


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


class Clients:
    """One stubbed boto3 client per service, created when the code asks for it."""

    def __init__(self):
        self.made = {}

    def __call__(self, service, region):
        if service not in self.made:
            client = boto3.client(service, region_name=region)
            stub = Stubber(client)
            stub.activate()
            self.made[service] = (client, stub)
        return self.made[service][0]

    def stub(self, service):
        self(service, REGION)
        return self.made[service][1]

    def done(self):
        for _, stub in self.made.values():
            stub.assert_no_pending_responses()


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def run(destroyed, clients, *, dry_run=False, env=None):
    said = []
    clock = Clock()
    destroys.prepare(
        destroyed,
        said.append,
        dry_run=dry_run,
        clients=clients,
        sleep=clock.sleep,
        now=clock.now,
        env=env,
    )
    return said


def deleted(kind, address, before):
    return {"address": f"{kind}.{address}", "type": kind, "before": before}


def build_tags(**extra):
    return {
        "managed_by": "fluid",
        "fluid_contract": "commerce_orders_stream",
        names.PRODUCT_TAG: PRODUCT,
        names.BUILD_TAG: BUILD,
        **extra,
    }


def log_group(runtime, target, **extra):
    """The build's log group as the state holds it, with the tags the emit gives it."""
    tags = build_tags(
        **{
            names.RUNTIME_TAG: runtime,
            names.RUN_TARGET_TAG: target,
            names.RUN_REGION_TAG: REGION,
        },
        **extra,
    )
    name = names.log_group_name(PRODUCT, BUILD)
    before = {"name": name, "region": REGION, "tags": tags, "tags_all": tags}
    return deleted("aws_cloudwatch_log_group", KEY, before)


def job_policy(**tags):
    return deleted(
        "aws_iam_policy",
        KEY,
        {"arn": POLICY_ARN, "name": POLICY_ARN.rsplit("/", 1)[1], "tags": build_tags(**tags)},
    )


def no_holders(clients, arn=POLICY_ARN):
    clients.stub("iam").add_response(
        "list_entities_for_policy",
        {"PolicyGroups": [], "PolicyUsers": [], "PolicyRoles": [], "IsTruncated": False},
        {"PolicyArn": arn},
    )


# --- the emit says where each build's runs are ------------------------------------------


@pytest.mark.parametrize(
    ("runtime", "target"),
    [
        ("glue", f"glueJobName={names.glue_job_name(PRODUCT, BUILD)}"),
        ("emr-eks", f"virtualClusterId={VIRTUAL_CLUSTER}"),
        ("emr-ec2", "clusterId=j-1ABCDEF"),
        (
            "emr-serverless",
            "applicationId=${aws_emrserverless_application.commerce_orders_stream_spark.id}",
        ),
    ],
)
def test_the_log_group_says_where_the_builds_runs_are(contract, runtime, target):
    resources, _ = emit(_runtime(contract, runtime))
    tags = resources["aws_cloudwatch_log_group"][KEY]["tags"]
    assert tags[names.RUNTIME_TAG] == runtime
    assert tags[names.RUN_TARGET_TAG] == target
    assert tags[names.RUN_REGION_TAG] == REGION
    assert tags[names.BUILD_TAG] == BUILD
    # Only the log group carries them: the build's other resources keep their tags.
    for objects in resources["aws_s3_object"].values():
        assert names.RUNTIME_TAG not in objects["tags"]


def test_a_domains_application_is_named_by_its_id(contract):
    resources, _ = emit(contract)
    tags = resources["aws_cloudwatch_log_group"][KEY]["tags"]
    assert tags[names.RUN_TARGET_TAG] == f"applicationId={APP}"


def test_what_the_emit_writes_is_what_a_destroy_reads(contract):
    """The tags of an emitted log group, read back as the plan's delete, find the runs where
    the run lane starts them: the cluster, and the build's run name."""
    resources, _ = emit(_runtime(contract, "emr-ec2"))
    group = resources["aws_cloudwatch_log_group"][KEY]
    found = destroys._read([deleted("aws_cloudwatch_log_group", KEY, {**group, "region": REGION})])
    (located,) = found.runs.values()
    assert (located.runtime, located.region, dict(located.target)) == (
        "emr-ec2",
        REGION,
        {"clusterId": "j-1ABCDEF"},
    )
    assert located.run_name == RUN_NAME


# --- a build that goes is stopped first ---------------------------------------------------


def _step(state):
    return {
        "Id": "s-1",
        "Name": RUN_NAME,
        "Config": {"Jar": "command-runner.jar", "Args": ["spark-submit"]},
        "ActionOnFailure": "CONTINUE",
        "Status": {"State": state, "Timeline": {"CreationDateTime": NOW}},
    }


def _ec2_running(clients, cluster="j-1ABCDEF"):
    emr = clients.stub("emr")
    active = {"ClusterId": cluster, "StepStates": ["PENDING", "RUNNING", "CANCEL_PENDING"]}
    emr.add_response("list_steps", {"Steps": [_step("RUNNING")]}, active)
    emr.add_response("list_steps", {"Steps": [_step("RUNNING")]}, {"ClusterId": cluster})
    return emr


def test_a_step_that_ended_by_itself_after_it_was_listed_needs_no_cancel():
    clients = Clients()
    emr = _ec2_running(clients)
    emr.add_response(
        "cancel_steps",
        {"CancelStepsInfoList": [{"StepId": "s-1", "Status": "FAILED", "Reason": "done"}]},
        {"ClusterId": "j-1ABCDEF", "StepIds": ["s-1"], "StepCancellationOption": "SEND_INTERRUPT"},
    )
    emr.add_response(
        "describe_step", {"Step": _step("COMPLETED")}, {"ClusterId": "j-1ABCDEF", "StepId": "s-1"}
    )
    assert run([log_group("emr-ec2", "clusterId=j-1ABCDEF")], clients) == []
    clients.done()


def test_an_emr_on_ec2_build_that_goes_has_its_step_cancelled_first():
    clients = Clients()
    no_holders(clients)
    emr = _ec2_running(clients)
    emr.add_response(
        "cancel_steps",
        {"CancelStepsInfoList": [{"StepId": "s-1", "Status": "SUBMITTED"}]},
        {"ClusterId": "j-1ABCDEF", "StepIds": ["s-1"], "StepCancellationOption": "SEND_INTERRUPT"},
    )
    emr.add_response(
        "describe_step", {"Step": _step("CANCELLED")}, {"ClusterId": "j-1ABCDEF", "StepId": "s-1"}
    )
    # The policy an earlier forge wrote carries no target: the log group's is enough.
    said = run([log_group("emr-ec2", "clusterId=j-1ABCDEF"), job_policy()], clients)
    clients.done()
    assert said == [
        f"  Spark {BUILD} (emr-ec2): stopped on EMR cluster j-1ABCDEF. cancelled s-1 before "
        "the apply destroys what it runs with"
    ]


IDENTITY = {
    names.RUNTIME_TAG: "emr-ec2",
    names.RUN_TARGET_TAG: "clusterId=j-1ABCDEF",
    names.RUN_REGION_TAG: REGION,
}


def test_the_job_policy_says_where_its_steps_run(contract):
    resources, _ = emit(_runtime(contract, "emr-ec2"))
    tags = resources["aws_iam_policy"][KEY]["tags"]
    assert {k: tags[k] for k in IDENTITY} == IDENTITY


def test_a_build_moved_off_emr_on_ec2_has_its_steps_cancelled_though_its_log_group_stays():
    """Its job policy is what goes; the log group stays, and its tags now name the new
    runtime."""
    clients = Clients()
    no_holders(clients)
    emr = _ec2_running(clients)
    emr.add_response(
        "cancel_steps",
        {"CancelStepsInfoList": [{"StepId": "s-1", "Status": "SUBMITTED"}]},
        {"ClusterId": "j-1ABCDEF", "StepIds": ["s-1"], "StepCancellationOption": "SEND_INTERRUPT"},
    )
    emr.add_response(
        "describe_step", {"Step": _step("CANCELLED")}, {"ClusterId": "j-1ABCDEF", "StepId": "s-1"}
    )
    said = run([job_policy(**IDENTITY)], clients)
    clients.done()
    assert said == [
        f"  Spark {BUILD} (emr-ec2): stopped on EMR cluster j-1ABCDEF. cancelled s-1 before "
        "the apply destroys what it runs with"
    ]


def test_an_attached_job_policy_refuses_before_anything_is_stopped():
    clients = Clients()
    clients.stub("iam").add_response(
        "list_entities_for_policy",
        {
            "PolicyGroups": [],
            "PolicyUsers": [],
            "PolicyRoles": [{"RoleName": "EMR_EC2_DefaultRole", "RoleId": "AROAEXAMPLE12345"}],
            "IsTruncated": False,
        },
        {"PolicyArn": POLICY_ARN},
    )
    with pytest.raises(UnsupportedBindingError) as caught:
        run([log_group("emr-ec2", "clusterId=j-1ABCDEF"), job_policy()], clients)
    assert caught.value.kind == "spark-policy-attached"
    assert "attached to role EMR_EC2_DefaultRole" in str(caught.value)
    assert "instance-profile role" in str(caught.value)
    assert "Nothing was stopped or destroyed" in str(caught.value)
    assert (
        f"aws iam detach-role-policy --role-name EMR_EC2_DefaultRole --policy-arn {POLICY_ARN}"
        in caught.value.remediation
    )
    assert set(clients.made) == {"iam"}  # no run was looked up, let alone cancelled
    clients.done()


def test_an_attached_caller_policy_names_the_schedulers_role():
    arn = "arn:aws:iam::000000000005:policy/fluid-commerce.orders-stream-spark-caller"
    caller = deleted(
        "aws_iam_policy",
        "commerce_orders_stream_spark_caller",
        {
            "arn": arn,
            "name": arn.rsplit("/", 1)[1],
            "tags": {"managed_by": "fluid", names.PRODUCT_TAG: PRODUCT},
        },
    )
    clients = Clients()
    clients.stub("iam").add_response(
        "list_entities_for_policy",
        {
            "PolicyGroups": [{"GroupName": "schedulers", "GroupId": "AGPAEXAMPLE12345"}],
            "PolicyUsers": [],
            "PolicyRoles": [],
            "IsTruncated": False,
        },
        {"PolicyArn": arn},
    )
    with pytest.raises(UnsupportedBindingError) as caught:
        run([caller], clients)
    assert "caller policy" in str(caught.value) and "group schedulers" in str(caught.value)
    assert (
        f"aws iam detach-group-policy --group-name schedulers --policy-arn {arn}"
        in caught.value.remediation
    )


def test_an_emr_on_eks_build_that_goes_has_its_job_run_cancelled():
    clients = Clients()
    eks = clients.stub("emr-containers")
    job = {
        "id": "e-1",
        "name": RUN_NAME,
        "virtualClusterId": VIRTUAL_CLUSTER,
        "state": "RUNNING",
        "createdAt": NOW,
    }
    where = {"id": "e-1", "virtualClusterId": VIRTUAL_CLUSTER}
    eks.add_response(
        "list_job_runs",
        {"jobRuns": [job]},
        {"virtualClusterId": VIRTUAL_CLUSTER, "name": RUN_NAME},
    )
    eks.add_response("cancel_job_run", where, where)
    eks.add_response("describe_job_run", {"jobRun": {**job, "state": "CANCELLED"}}, where)
    said = run([log_group("emr-eks", f"virtualClusterId={VIRTUAL_CLUSTER}")], clients)
    clients.done()
    assert "cancelled e-1" in said[0]


def test_a_glue_job_that_goes_has_its_runs_stopped_even_when_its_build_stays():
    """A build moved off Glue keeps its log group; its Glue job going is what says so."""
    job_name = names.glue_job_name(PRODUCT, BUILD)
    glue_job = deleted(
        "aws_glue_job",
        KEY,
        {
            "name": job_name,
            "arn": f"arn:aws:glue:{REGION}:000000000005:job/{job_name}",
            "tags": build_tags(),
        },
    )
    clients = Clients()
    glue = clients.stub("glue")
    running = {"Id": "jr_1", "JobName": job_name, "JobRunState": "RUNNING", "StartedOn": NOW}
    glue.add_response(
        "get_job_runs", {"JobRuns": [running]}, {"JobName": job_name, "MaxResults": 25}
    )
    glue.add_response(
        "batch_stop_job_run",
        {"SuccessfulSubmissions": [{"JobName": job_name, "JobRunId": "jr_1"}], "Errors": []},
        {"JobName": job_name, "JobRunIds": ["jr_1"]},
    )
    glue.add_response(
        "get_job_run",
        {"JobRun": {**running, "JobRunState": "STOPPED"}},
        {"JobName": job_name, "RunId": "jr_1"},
    )
    # Both say the same Glue job: its runs are looked up once.
    said = run([glue_job, log_group("glue", f"glueJobName={job_name}")], clients)
    clients.done()
    assert said == [
        f"  Spark {BUILD} (glue): stopped on Glue job {job_name}. cancelled jr_1 before the "
        "apply destroys what it runs with"
    ]


def _job_run(state):
    return {
        "jobRun": {
            "applicationId": APP,
            "jobRunId": "jr1",
            "arn": JOB_ARN,
            "createdBy": USER,
            "createdAt": NOW,
            "updatedAt": NOW,
            "executionRole": USER,
            "state": state,
            "stateDetails": "running",
            "releaseLabel": "emr-7.12.0",
            "jobDriver": {"sparkSubmit": {"entryPoint": "s3://b/runner.py"}},
            "name": "someone-elses-run",
        }
    }


def _application(state):
    return {
        "application": {
            "applicationId": APP,
            "name": "fluid-commerce.orders-stream-spark",
            "arn": f"arn:aws:emr-serverless:{REGION}:000000000005:/applications/{APP}",
            "releaseLabel": "emr-7.12.0",
            "type": "Spark",
            "state": state,
            "createdAt": NOW,
            "updatedAt": NOW,
        }
    }


def _summary():
    job = _job_run("RUNNING")["jobRun"]
    keep = (
        "applicationId",
        "arn",
        "createdBy",
        "createdAt",
        "updatedAt",
        "executionRole",
        "state",
        "stateDetails",
        "releaseLabel",
        "name",
    )
    return {"id": "jr1", **{k: job[k] for k in keep}}


ACTIVE = sorted(["SUBMITTED", "PENDING", "SCHEDULED", "QUEUED", "RUNNING", "CANCELLING"])
APP_ID = {"applicationId": APP}


def application_goes():
    return deleted(
        "aws_emrserverless_application",
        "commerce_orders_stream_spark",
        {
            "id": APP,
            "name": "fluid-commerce.orders-stream-spark",
            "region": REGION,
            "tags": {"managed_by": "fluid", names.PRODUCT_TAG: PRODUCT},
        },
    )


def test_the_products_application_is_emptied_and_stopped_so_it_can_be_deleted():
    clients = Clients()
    emr = clients.stub("emr-serverless")
    emr.add_response("list_job_runs", {"jobRuns": [_summary()]}, {**APP_ID, "states": ACTIVE})
    emr.add_response("get_job_run", _job_run("RUNNING"), {**APP_ID, "jobRunId": "jr1"})
    emr.add_response("get_application", _application("STARTED"), APP_ID)
    emr.add_response("cancel_job_run", {**APP_ID, "jobRunId": "jr1"}, {**APP_ID, "jobRunId": "jr1"})
    emr.add_response("get_job_run", _job_run("CANCELLED"), {**APP_ID, "jobRunId": "jr1"})
    # Read again after the wait, which may have been long.
    emr.add_response("get_application", _application("STARTED"), APP_ID)
    emr.add_response("stop_application", {}, APP_ID)
    emr.add_response("get_application", _application("STOPPED"), APP_ID)
    # The build's own runs are in that application: they go with it, not by name first.
    said = run([application_goes(), log_group("emr-serverless", f"applicationId={APP}")], clients)
    clients.done()
    assert said == [
        f"  EMR Serverless application fluid-commerce.orders-stream-spark ({APP}): cancelled "
        "jr1 before the apply destroys what it runs with; stopped the application."
    ]


def test_an_idle_started_application_is_stopped_and_a_stopped_one_left_alone():
    clients = Clients()
    emr = clients.stub("emr-serverless")
    emr.add_response("list_job_runs", {"jobRuns": []}, {**APP_ID, "states": ACTIVE})
    emr.add_response("get_application", _application("STARTED"), APP_ID)
    emr.add_response("stop_application", {}, APP_ID)
    emr.add_response("get_application", _application("STOPPING"), APP_ID)
    emr.add_response("get_application", _application("STOPPED"), APP_ID)
    assert "stopped the application" in run([application_goes()], clients)[0]
    clients.done()
    emr.add_response("list_job_runs", {"jobRuns": []}, {**APP_ID, "states": ACTIVE})
    emr.add_response("get_application", _application("STOPPED"), APP_ID)
    assert run([application_goes()], clients) == []
    clients.done()


def test_an_application_still_being_created_is_waited_for_and_then_left_alone():
    clients = Clients()
    emr = clients.stub("emr-serverless")
    emr.add_response("list_job_runs", {"jobRuns": []}, {**APP_ID, "states": ACTIVE})
    emr.add_response("get_application", _application("CREATING"), APP_ID)
    emr.add_response("get_application", _application("CREATING"), APP_ID)
    emr.add_response("get_application", _application("CREATED"), APP_ID)
    assert run([application_goes()], clients) == []  # a created one is deleted as it is
    clients.done()


def test_an_application_still_starting_is_stopped_once_it_has_started():
    clients = Clients()
    emr = clients.stub("emr-serverless")
    emr.add_response("list_job_runs", {"jobRuns": []}, {**APP_ID, "states": ACTIVE})
    emr.add_response("get_application", _application("STARTING"), APP_ID)
    emr.add_response("get_application", _application("STARTED"), APP_ID)
    emr.add_response("stop_application", {}, APP_ID)
    emr.add_response("get_application", _application("STOPPED"), APP_ID)
    said = run([application_goes()], clients)
    clients.done()
    assert said == [
        f"  EMR Serverless application fluid-commerce.orders-stream-spark ({APP}): stopped "
        "the application."
    ]


def test_an_application_still_creating_after_the_wait_refuses_the_apply():
    clients = Clients()
    emr = clients.stub("emr-serverless")
    emr.add_response("list_job_runs", {"jobRuns": []}, {**APP_ID, "states": ACTIVE})
    for _ in range(4):  # the first read, then at 0 s, 1 s and 3 s of a 3 s wait
        emr.add_response("get_application", _application("CREATING"), APP_ID)
    with pytest.raises(UnsupportedBindingError) as caught:
        run([application_goes()], clients, env={"FLUID_SPARK_WAIT_SECONDS": "3"})
    clients.done()  # nothing was stopped
    assert caught.value.kind == "spark-destroy-runs-going"
    assert "the application is still CREATING after 3 s" in str(caught.value)
    hints = " ".join(caught.value.remediation)
    for action in (
        "emr-serverless:GetApplication",
        "emr-serverless:StopApplication",
        "iam:ListEntitiesForPolicy",
    ):
        assert action in hints


# --- dry runs, errors, timeouts --------------------------------------------------------


def test_a_dry_run_reads_and_says_what_it_would_cancel_and_changes_nothing():
    clients = Clients()
    no_holders(clients)
    _ec2_running(clients)
    said = run([log_group("emr-ec2", "clusterId=j-1ABCDEF"), job_policy()], clients, dry_run=True)
    clients.done()  # only reads were stubbed: a cancel would have failed the call
    assert said == [
        f"  Spark {BUILD} (emr-ec2): planned on EMR cluster j-1ABCDEF. would cancel s-1"
    ]


def test_a_dry_run_says_it_would_stop_the_application():
    clients = Clients()
    emr = clients.stub("emr-serverless")
    emr.add_response("list_job_runs", {"jobRuns": []}, {**APP_ID, "states": ACTIVE})
    emr.add_response("get_application", _application("STARTED"), APP_ID)
    said = run([application_goes()], clients, dry_run=True)
    clients.done()
    assert said == [
        f"  EMR Serverless application fluid-commerce.orders-stream-spark ({APP}): would stop "
        "the application (STARTED)."
    ]


def test_a_run_still_going_after_the_wait_refuses_the_apply():
    clients = Clients()
    emr = _ec2_running(clients)
    emr.add_response(
        "cancel_steps",
        {"CancelStepsInfoList": [{"StepId": "s-1", "Status": "SUBMITTED"}]},
        {"ClusterId": "j-1ABCDEF", "StepIds": ["s-1"], "StepCancellationOption": "SEND_INTERRUPT"},
    )
    for _ in range(3):  # read at 0 s, 1 s and 3 s of a 3 s wait
        emr.add_response(
            "describe_step",
            {"Step": _step("CANCEL_PENDING")},
            {"ClusterId": "j-1ABCDEF", "StepId": "s-1"},
        )
    with pytest.raises(UnsupportedBindingError) as caught:
        run(
            [log_group("emr-ec2", "clusterId=j-1ABCDEF")],
            clients,
            env={"FLUID_SPARK_WAIT_SECONDS": "3"},
        )
    clients.done()
    assert caught.value.kind == "spark-destroy-runs-going"
    assert "run s-1 was cancelled but is still CANCEL_PENDING after 3 s" in str(caught.value)
    assert "nothing was destroyed" in str(caught.value)


def test_an_aws_error_refuses_the_apply_and_is_a_note_in_a_dry_run():
    def denied(clients):
        clients.stub("emr").add_client_error(
            "list_steps", service_error_code="AccessDeniedException", service_message="denied"
        )

    clients = Clients()
    denied(clients)
    with pytest.raises(UnsupportedBindingError, match="AccessDeniedException"):
        run([log_group("emr-ec2", "clusterId=j-1ABCDEF")], clients)
    clients = Clients()
    denied(clients)
    said = run([log_group("emr-ec2", "clusterId=j-1ABCDEF")], clients, dry_run=True)
    assert said[0].startswith("  note: the apply would stop the runs first, and could not read")


def test_a_target_that_is_gone_holds_no_run():
    clients = Clients()
    clients.stub("emr-serverless").add_client_error(
        "list_job_runs", service_error_code="ResourceNotFoundException"
    )
    assert run([log_group("emr-serverless", "applicationId=00fgone")], clients) == []
    clients.done()


def test_a_build_an_earlier_forge_applied_is_one_note_naming_the_run():
    tags = build_tags()  # no runtime: written before its resources said where runs are
    name = names.log_group_name(PRODUCT, BUILD)
    group = deleted("aws_cloudwatch_log_group", KEY, {"name": name, "region": REGION, "tags": tags})
    clients = Clients()
    no_holders(clients)
    said = run([group, job_policy()], clients)
    assert said == [
        f"  note: Spark build {BUILD} goes, but nothing it is destroyed with says where it runs "
        f"(an apply by an earlier forge): stop its run {RUN_NAME} yourself if one is going."
    ]
    assert set(clients.made) == {"iam"}  # the policy is checked; no run is looked up
    clients.done()


def test_only_the_removed_builds_runs_stop_in_an_application_that_stays():
    """A domain's shared EMR Serverless application: the other runs in it go on."""
    ours = {**_summary(), "id": "jr1", "name": RUN_NAME}
    theirs = {**_summary(), "id": "jr9", "name": "fluid-other.product-load-1a2b3c4d"}
    clients = Clients()
    emr = clients.stub("emr-serverless")
    emr.add_response("list_job_runs", {"jobRuns": [ours, theirs]}, {**APP_ID, "states": ACTIVE})
    emr.add_response("list_job_runs", {"jobRuns": [ours, theirs]}, APP_ID)
    emr.add_response("get_job_run", _job_run("RUNNING"), {**APP_ID, "jobRunId": "jr1"})
    emr.add_response("cancel_job_run", {**APP_ID, "jobRunId": "jr1"}, {**APP_ID, "jobRunId": "jr1"})
    emr.add_response("get_job_run", _job_run("CANCELLED"), {**APP_ID, "jobRunId": "jr1"})
    said = run([log_group("emr-serverless", f"applicationId={APP}")], clients)
    clients.done()  # jr9 was never cancelled: no call for it was stubbed
    assert said == [
        f"  Spark {BUILD} (emr-serverless): stopped on EMR Serverless application {APP}. "
        "cancelled jr1 before the apply destroys what it runs with"
    ]


def test_a_plan_without_spark_destroys_asks_aws_nothing():
    clients = Clients()
    bucket = deleted("aws_s3_bucket", "lake", {"bucket": "b", "tags": {"managed_by": "fluid"}})
    assert run([bucket, deleted("aws_glue_catalog_table", "t", {})], clients) == []
    assert clients.made == {}


# --- the plugin and the engine -----------------------------------------------------------


def test_the_aws_plugin_hands_the_plans_deletes_to_the_hook():
    clients = Clients()
    no_holders(clients)
    _ec2_running(clients)
    said = []
    get_iac_plugin("aws").prepare_destroys(
        CONTRACT,
        [log_group("emr-ec2", "clusterId=j-1ABCDEF"), job_policy()],
        say=said.append,
        dry_run=True,
        client_factory=clients,
    )
    clients.done()
    assert "would cancel s-1" in said[0]


def test_the_engine_turns_a_refusal_into_the_applys_error(monkeypatch):
    from fluid_build.build_runners.spark import _adapters
    from fluid_build.cli import _apply_opentofu_engine as engine
    from fluid_build.cli._common import CLIError

    clients = Clients()
    clients.stub("iam").add_response(
        "list_entities_for_policy",
        {
            "PolicyGroups": [],
            "PolicyUsers": [{"UserName": "ops", "UserId": "AIDAEXAMPLE12345"}],
            "PolicyRoles": [],
            "IsTruncated": False,
        },
        {"PolicyArn": POLICY_ARN},
    )
    monkeypatch.setattr(_adapters, "default_clients", clients)
    with pytest.raises(CLIError) as caught:
        engine._prepare_destroys(get_iac_plugin("aws"), CONTRACT, [job_policy()])
    assert caught.value.event == "opentofu_destroy_preparation_failed"
    assert caught.value.context["kind"] == "spark-policy-attached"
