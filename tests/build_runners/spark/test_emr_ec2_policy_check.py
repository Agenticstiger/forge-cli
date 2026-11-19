# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An EMR on EC2 step runs as the cluster's instance-profile role: forge starts none until the
build's least-privilege policy is attached to that role (``_adapters.EmrEc2.start``). Every
AWS call goes through botocore's Stubber, which checks it against the real API model."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

boto3 = pytest.importorskip("boto3")
Stubber = pytest.importorskip("botocore.stub").Stubber

from fluid_build.build_runners.spark import _adapters as ad
from fluid_build.build_runners.spark import _reconcile
from fluid_build.spark import names
from tests.build_runners.spark.conftest import variant
from tests.build_runners.spark.test_adapters import ConfigBucket, started

CLUSTER = "j-1ABCDEF"
PROFILE = "EMR_EC2_DefaultRole"
ROLE = "EMR_EC2_DefaultRole"
NOW = dt.datetime(2026, 11, 19, 6, 0, tzinfo=dt.timezone.utc)


class Clients:
    """One stubbed boto3 client per service, created when the code asks for it; S3 is the
    bucket a start reads the artifacts back from (``ConfigBucket``)."""

    def __init__(self, s3=None):
        self.made, self.s3 = {}, s3

    def __call__(self, service, region):
        if service == "s3" and self.s3 is not None:
            return self.s3
        if service not in self.made:
            client = boto3.client(service, region_name=region)
            stub = Stubber(client)
            stub.activate()
            self.made[service] = (client, stub)
        return self.made[service][0]

    def stub(self, service, region="eu-west-1"):
        self(service, region)
        return self.made[service][1]

    def done(self):
        for _, stub in self.made.values():
            stub.assert_no_pending_responses()


@pytest.fixture
def ec2_run(run):
    return variant(run, "emr-ec2", {"clusterId": CLUSTER})


def policy_name(run):
    return names.execution_role_name(run.product, run.build)


def _role_reads(clients, run, attached):
    emr = clients.stub("emr")
    emr.add_response(
        "describe_cluster",
        {
            "Cluster": {
                "Id": CLUSTER,
                "Name": "lake",
                "Status": {"State": "WAITING"},
                "Ec2InstanceAttributes": {"IamInstanceProfile": PROFILE},
            }
        },
        {"ClusterId": CLUSTER},
    )
    iam = clients.stub("iam")
    iam.add_response(
        "get_instance_profile",
        {
            "InstanceProfile": {
                "Path": "/",
                "InstanceProfileName": PROFILE,
                "InstanceProfileId": "AIPAEXAMPLE12345",
                "Arn": f"arn:aws:iam::123456789012:instance-profile/{PROFILE}",
                "CreateDate": NOW,
                "Roles": [
                    {
                        "Path": "/",
                        "RoleName": ROLE,
                        "RoleId": "AROAEXAMPLE12345",
                        "Arn": f"arn:aws:iam::123456789012:role/{ROLE}",
                        "CreateDate": NOW,
                    }
                ],
            }
        },
        {"InstanceProfileName": PROFILE},
    )
    iam.add_response(
        "list_attached_role_policies",
        {
            "AttachedPolicies": [
                {"PolicyName": name, "PolicyArn": f"arn:aws:iam::123456789012:policy/{name}"}
                for name in attached
            ],
            "IsTruncated": False,
        },
        {"RoleName": ROLE},
    )
    return emr


def _step_added(emr, run):
    args = ["spark-submit", "--deploy-mode", "client", *ad._conf_params(run.spark_conf)]
    args += [run.runner_uri, *started(run).entry_args()]
    emr.add_response(
        "add_job_flow_steps",
        {"StepIds": ["s-1"]},
        {
            "JobFlowId": CLUSTER,
            "Steps": [
                {
                    "Name": run.run_name,
                    "ActionOnFailure": "CONTINUE",
                    "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args},
                }
            ],
        },
    )


def artifacts(run):
    return ConfigBucket(run.job_text.encode("utf-8"))


def test_a_step_starts_once_its_policy_is_on_the_clusters_role(ec2_run):
    clients = Clients(artifacts(ec2_run))
    emr = _role_reads(clients, ec2_run, ["AmazonElasticMapReduceforEC2Role", policy_name(ec2_run)])
    _step_added(emr, ec2_run)
    assert ad.adapter_for(ec2_run, clients).start("tok") == "s-1"
    clients.done()


def test_no_step_starts_before_its_policy_is_attached(ec2_run):
    clients = Clients()
    _role_reads(clients, ec2_run, ["AmazonElasticMapReduceforEC2Role"])
    with pytest.raises(ad.SparkRunError) as caught:
        ad.adapter_for(ec2_run, clients).start("tok")
    clients.done()  # no add_job_flow_steps was stubbed, nor made
    policy = policy_name(ec2_run)
    arn = f"arn:aws:iam::123456789012:policy/{policy}"
    assert str(caught.value) == (
        f"not started: policy {policy} is not attached to {ROLE}, the role its steps run as on "
        f"{CLUSTER}. Attach it, then apply again: aws iam attach-role-policy --role-name {ROLE} "
        f"--policy-arn {arn}"
    )


def test_a_step_starts_as_before_when_the_role_cannot_be_read(ec2_run):
    clients = Clients(artifacts(ec2_run))
    emr = clients.stub("emr")
    emr.add_client_error("describe_cluster", service_error_code="AccessDeniedException")
    _step_added(emr, ec2_run)
    assert ad.adapter_for(ec2_run, clients).start("tok") == "s-1"
    clients.done()


def test_a_named_role_has_no_forge_policy_to_check(ec2_run):
    named = dataclasses.replace(ec2_run, role_arn="arn:aws:iam::123456789012:role/data/steps")
    clients = Clients(artifacts(named))
    _step_added(clients.stub("emr"), named)
    assert ad.adapter_for(named, clients).start("tok") == "s-1"
    clients.done()  # no describe_cluster, no IAM read


def test_the_reconcile_starts_nothing_and_says_how_to_attach(ec2_run):
    """The refusal is the build's error outcome; no run was started, so once the policy is
    attached the next apply starts the stream (a failed run would not start again)."""
    clients = Clients()
    emr = clients.stub("emr")
    active = {"ClusterId": CLUSTER, "StepStates": ["PENDING", "RUNNING", "CANCEL_PENDING"]}
    emr.add_response("list_steps", {"Steps": []}, active)
    emr.add_response("list_steps", {"Steps": []}, {"ClusterId": CLUSTER})
    _role_reads(clients, ec2_run, [])
    (out,) = _reconcile.reconcile([ec2_run], clients=clients)
    clients.done()
    assert out.error and out.run_id is None
    assert "not started: policy" in out.message and "aws iam attach-role-policy" in out.message
