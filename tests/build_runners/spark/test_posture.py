# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The EMR on EC2 cluster posture warning: the rules, the reads (botocore's Stubber, which
checks each request and response against the real EMR API model, and moto), and the
``fluid plan`` / ``fluid apply`` hooks. A warning never refuses, and a cluster that cannot
be read costs a debug line only."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging

import pytest
import yaml

from fluid_build.build_runners.spark import _posture as po
from fluid_build.build_runners.spark._desired import REGION_TOKEN
from fluid_build.build_runners.spark._posture import AM_UNPINNED, CORE_SPOT, PRIMARY_SPOT
from fluid_build.cli import _spark_apply as _apply
from tests.build_runners.spark.conftest import spark_cfg, variant

try:  # the [aws] extra: a lane without it runs the rules only
    import boto3
    from botocore.stub import Stubber
except ImportError:  # pragma: no cover
    boto3 = Stubber = None
aws = pytest.mark.skipif(boto3 is None, reason="needs the [aws] extra (boto3)")

LOG = logging.getLogger("test.posture")
ON = {po.ENABLED: "true", po.AM_LABEL: "ON_DEMAND"}
CLUSTER = {"ClusterId": "j-ABC"}


def posture(release=(7, 12, 0), spot=(), yarn=None, fleets=False):
    return po.Posture("j-ABC", fleets, release, frozenset(spot), {} if yarn is None else yarn)


def codes(p):
    return [code for code, _, _ in po.evaluate(p)]


def text_of(p, code):
    (found,) = [(problem, fix) for c, problem, fix in po.evaluate(p) if c == code]
    return found


def forbidden(*a, **k):
    raise AssertionError("AWS was touched")


@pytest.fixture
def ec2(run):
    return variant(run, "emr-ec2", {"clusterId": "j-ABC"})


# --- the rules ------------------------------------------------------------------------


def test_spot_task_nodes_with_masters_on_on_demand_need_no_warning():
    assert codes(posture(spot={"TASK"}, yarn=ON)) == []


def test_a_cluster_without_spot_needs_no_warning_whatever_its_labels():
    assert codes(posture(spot=(), yarn={})) == []


def test_a_spot_primary_is_named_with_its_fix_and_masters_never_run_there():
    assert codes(posture(spot={"MASTER"})) == [PRIMARY_SPOT]
    problem, fix = text_of(posture(spot={"MASTER"}), PRIMARY_SPOT)
    assert "--deploy-mode client" in problem
    assert fix.startswith("create the cluster with an On-Demand primary node")


@pytest.mark.parametrize(
    ("fleets", "fix"),
    [
        (False, "On-Demand core instance group"),
        (True, "TargetSpotCapacity 0, with aws emr modify-instance-fleet"),
    ],
)
def test_spot_core_nodes_are_named_with_the_fix_for_groups_and_fleets(fleets, fix):
    p = posture(spot={"CORE"}, yarn=ON, fleets=fleets)
    assert codes(p) == [CORE_SPOT]
    assert fix in text_of(p, CORE_SPOT)[1]


@pytest.mark.parametrize(
    ("release", "yarn", "spot", "why"),
    [
        ((7, 12, 0), {}, {"TASK"}, f"{po.ENABLED} is unset"),
        ((7, 12, 0), {po.ENABLED: "false", po.AM_LABEL: "ON_DEMAND"}, {"TASK"}, "is false"),
        ((7, 12, 0), {po.ENABLED: "true"}, {"TASK"}, f"{po.AM_LABEL} is unset"),
        ((7, 12, 0), {po.ENABLED: "true", po.AM_LABEL: "CORE"}, {"CORE"}, "core nodes run"),
        ((6, 15, 0), {}, {"TASK"}, f"{po.ENABLED} is unset"),
        ((6, 15, 0), ON, {"TASK"}, "from release 7.0 only"),
        ((5, 36, 2), {}, {"CORE", "TASK"}, "is CORE, and core nodes run on Spot"),
        ((5, 36, 2), {po.ENABLED: "false"}, {"TASK"}, "is false"),
        ((7, 12, 0), {po.ENABLED: "true", po.AM_LABEL: "GPU"}, {"TASK"}, "is GPU"),
    ],
)
def test_masters_that_can_land_on_spot_are_named_with_the_setting_seen(release, yarn, spot, why):
    p = posture(release=release, spot=spot, yarn=yarn)
    assert AM_UNPINNED in codes(p)
    assert why in text_of(p, AM_UNPINNED)[0]


@pytest.mark.parametrize(
    ("release", "yarn"),
    [
        ((7, 12, 0), ON),
        ((7, 0, 0), ON),
        ((7, 12, 0), {po.ENABLED: "True", po.AM_LABEL: " ON_DEMAND "}),
        ((7, 12, 0), {po.ENABLED: "true", po.AM_LABEL: "CORE"}),
        ((6, 15, 0), {po.ENABLED: "true", po.AM_LABEL: "CORE"}),
        # EMR 5.19 to 5.x keep masters on CORE by default.
        ((5, 36, 2), {}),
        ((5, 19, 0), {}),
        (None, ON),
    ],
)
def test_masters_pinned_to_on_demand_nodes_need_no_warning(release, yarn):
    assert AM_UNPINNED not in codes(posture(release=release, spot={"TASK"}, yarn=yarn))


@pytest.mark.parametrize(
    ("release", "label"),
    [((7, 2, 0), "ON_DEMAND"), (None, "ON_DEMAND"), ((7, 1, 0), "CORE"), ((6, 15, 0), "CORE")],
)
def test_the_masters_fix_names_the_label_the_release_supports(release, label):
    _, fix = text_of(posture(release=release, spot={"TASK"}), AM_UNPINNED)
    assert f"{po.AM_LABEL}={label}" in fix and f"{po.ENABLED}=true" in fix
    assert ("with On-Demand core nodes" in fix) == (label == "CORE")


def test_the_masters_fix_names_the_primary_fleet_on_a_fleet_cluster():
    _, fix = text_of(posture(spot={"TASK"}, fleets=True), AM_UNPINNED)
    assert fix.endswith("by reconfiguring its primary fleet.")


def test_unknown_primary_settings_give_no_masters_verdict(caplog):
    p = dataclasses.replace(posture(spot={"CORE"}), yarn_site=None)
    with caplog.at_level(logging.DEBUG, logger=po.LOG.name):
        assert codes(p) == [CORE_SPOT]
    assert "spark_cluster_posture_am_unknown" in caplog.text


def test_a_value_read_from_the_cluster_is_quoted_when_it_holds_a_control_character():
    p = posture(spot={"TASK"}, yarn={po.ENABLED: "true", po.AM_LABEL: "GPU\x1b[2J"})
    problem, _ = text_of(p, AM_UNPINNED)
    assert "\x1b" not in problem and "'GPU\\x1b[2J'" in problem
    p = posture(spot={"TASK"}, yarn={po.ENABLED: "no\x07"})
    assert "'no\\x07'" in text_of(p, AM_UNPINNED)[0]


@pytest.mark.parametrize(
    ("label", "release"),
    [("emr-7.12.0", (7, 12, 0)), ("emr-5.36.2", (5, 36, 2)), ("custom", None), (None, None)],
)
def test_release_labels_are_read_as_numbers(label, release):
    assert po._release(label) == release


def test_a_finding_reads_as_its_problem_then_its_fix():
    one = po.Finding(CORE_SPOT, "j-ABC", ("ingest",), "core nodes run on Spot.", "do this.")
    assert one.line() == (
        "EMR cluster j-ABC (Spark build ingest): core nodes run on Spot.\n    Fix: do this."
    )
    two = dataclasses.replace(one, builds=("a", "b"))
    assert two.line().startswith("EMR cluster j-ABC (Spark builds a, b): ")
    assert one.as_dict() == {
        "code": CORE_SPOT,
        "cluster": "j-ABC",
        "builds": ("ingest",),
        "problem": "core nodes run on Spot.",
        "fix": "do this.",
    }


# --- the reads, against the EMR API model ----------------------------------------------


@pytest.fixture
def emr():
    """An EMR client whose every request and response botocore checks; all stubbed answers
    must be used."""
    client = boto3.client("emr", region_name="eu-west-1")
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


def yarn_site(**props):
    return [{"Classification": "yarn-site", "Properties": props}]


def described(collection="INSTANCE_GROUP", configurations=(), state="WAITING"):
    return {
        "Cluster": {
            "Id": "j-ABC",
            "Status": {"State": state},
            "InstanceCollectionType": collection,
            "ReleaseLabel": "emr-7.12.0",
            "Configurations": list(configurations),
        }
    }


def group(kind, market="ON_DEMAND", state="RUNNING", **extra):
    return {
        "Id": f"ig-{kind}",
        "InstanceGroupType": kind,
        "Market": market,
        "Status": {"State": state},
        **extra,
    }


def fleet(kind, on_demand=0, spot=0, types=({},), provisioned_spot=0):
    return {
        "Id": f"if-{kind}",
        "InstanceFleetType": kind,
        "Status": {"State": "RUNNING"},
        "TargetOnDemandCapacity": on_demand,
        "TargetSpotCapacity": spot,
        "ProvisionedSpotCapacity": provisioned_spot,
        "InstanceTypeSpecifications": [
            {"InstanceType": "m5.xlarge", **({"Configurations": c} if c else {})} for c in types
        ],
    }


def answer(stub, cluster, *pages):
    """``DescribeCluster``, then each page of the cluster's group or fleet listing."""
    stub.add_response("describe_cluster", cluster, CLUSTER)
    fleets = cluster["Cluster"]["InstanceCollectionType"] == "INSTANCE_FLEET"
    operation, key = (
        ("list_instance_fleets", "InstanceFleets")
        if fleets
        else ("list_instance_groups", "InstanceGroups")
    )
    for number, items in enumerate(pages):
        response = {key: list(items)}
        if number + 1 < len(pages):
            response["Marker"] = f"m{number + 1}"
        request = {**CLUSTER, "Marker": f"m{number}"} if number else CLUSTER
        stub.add_response(operation, response, request)


def read(emr, cluster, *pages):
    client, stub = emr
    answer(stub, cluster, *pages)
    return po.read_posture("j-ABC", "eu-west-1", lambda service, region: client)


@aws
def test_the_primary_groups_applied_settings_override_the_clusters(emr):
    master = group("MASTER", Configurations=yarn_site(**{po.AM_LABEL: "ON_DEMAND"}))
    p = read(
        emr,
        described(configurations=yarn_site(**{po.ENABLED: "true"})),
        [master, group("CORE"), group("TASK", "SPOT")],
    )
    assert (p.fleets, p.release, p.spot) == (False, (7, 12, 0), frozenset({"TASK"}))
    assert p.yarn_site == ON
    assert po.evaluate(p) == []


@aws
def test_a_pending_reconfiguration_counts_the_settings_last_applied(emr):
    master = group(
        "MASTER",
        Configurations=yarn_site(**ON),
        ConfigurationsVersion=2,
        LastSuccessfullyAppliedConfigurations=[],
        LastSuccessfullyAppliedConfigurationsVersion=1,
    )
    assert codes(read(emr, described(), [master, group("TASK", "SPOT")])) == [AM_UNPINNED]


@aws
def test_groups_on_their_way_out_are_left_out_and_later_pages_are_read(emr):
    first = [group("MASTER"), group("TASK", "SPOT", "ENDED")]
    p = read(emr, described(configurations=yarn_site(**ON)), first, [group("CORE", "SPOT")])
    assert p.spot == frozenset({"CORE"})
    assert codes(p) == [CORE_SPOT]


@aws
def test_fleets_with_spot_capacity_on_the_core_are_named(emr):
    fleets = [
        fleet("MASTER", on_demand=1),
        fleet("CORE", on_demand=2, spot=2),
        fleet("TASK", provisioned_spot=4),
    ]
    p = read(emr, described("INSTANCE_FLEET"), fleets)
    assert (p.fleets, p.spot) == (True, frozenset({"CORE", "TASK"}))
    assert codes(p) == [CORE_SPOT, AM_UNPINNED]


@aws
@pytest.mark.parametrize(
    ("types", "yarn"),
    [((yarn_site(**ON), yarn_site(**ON)), ON), ((yarn_site(**ON), ()), None)],
    ids=["types-agree", "types-disagree"],
)
def test_the_primary_fleets_instance_types_count_only_when_they_agree(emr, types, yarn):
    fleets = [fleet("MASTER", on_demand=1, types=types), fleet("TASK", spot=4)]
    p = read(emr, described("INSTANCE_FLEET"), fleets)
    assert p.yarn_site == yarn
    assert codes(p) == []


@aws
@pytest.mark.parametrize("state", ["TERMINATING", "TERMINATED", "TERMINATED_WITH_ERRORS"])
def test_an_ending_cluster_is_not_assessed(emr, state):
    client, stub = emr
    stub.add_response("describe_cluster", described(state=state), CLUSTER)
    assert po.read_posture("j-ABC", "eu-west-1", lambda service, region: client) is None


@aws
def test_a_cluster_that_cannot_be_read_gets_a_debug_line_without_the_callers_arn(emr, ec2, caplog):
    client, stub = emr
    stub.add_client_error(
        "describe_cluster",
        service_error_code="AccessDeniedException",
        service_message="User: arn:aws:iam::123456789012:user/ci is not authorized",
        http_status_code=400,
    )
    with caplog.at_level(logging.DEBUG, logger=po.LOG.name):
        assert po.cluster_findings([ec2], clients=lambda s, r: client) == []
    assert "spark_cluster_posture_unread" in caplog.text
    assert "error=AccessDeniedException" in caplog.text
    assert "arn:aws:iam" not in caplog.text


def test_a_client_that_cannot_be_made_is_a_cluster_that_cannot_be_read(ec2):
    def no_boto(service, region):
        raise ImportError("boto3")

    assert po.cluster_findings([ec2], clients=no_boto) == []


@aws
def test_one_read_per_cluster_names_every_build_on_it(emr, ec2):
    client, stub = emr
    answer(stub, described(), [group("MASTER", "SPOT")])
    other = dataclasses.replace(ec2, build="load-refunds")
    found = po.cluster_findings([ec2, other], clients=lambda s, r: client)
    assert [(f.code, f.builds) for f in found] == [
        (PRIMARY_SPOT, ("ingest-orders", "load-refunds"))
    ]


def test_runs_elsewhere_and_a_region_nobody_names_read_nothing(run, ec2, monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    unplaced = dataclasses.replace(ec2, region=REGION_TOKEN)
    assert po.cluster_findings([run, unplaced], clients=forbidden) == []


def test_the_credentials_region_places_a_run_whose_output_names_none(ec2, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "ca-central-1")
    seen = []

    def record(service, region):
        seen.append((service, region))
        raise RuntimeError("stop here")

    po.cluster_findings([dataclasses.replace(ec2, region=REGION_TOKEN)], clients=record)
    assert seen == [("emr", "ca-central-1")]


@aws
def test_a_region_lookup_that_fails_any_way_reads_nothing(ec2, monkeypatch, caplog):
    """A named profile that does not exist raises botocore's own error, not ValueError."""
    from botocore.exceptions import ProfileNotFound

    def missing_profile():
        raise ProfileNotFound(profile="nope")

    monkeypatch.setattr("fluid_build.build_runners.spark._desired.resolve_region", missing_profile)
    unplaced = dataclasses.replace(ec2, region=REGION_TOKEN)
    with caplog.at_level(logging.DEBUG, logger=po.LOG.name):
        assert po.cluster_findings([unplaced], clients=forbidden) == []
    assert "error=ProfileNotFound" in caplog.text


@aws
def test_the_reads_are_bounded():
    config = po.posture_clients("emr", "eu-west-1").meta.config
    assert (config.connect_timeout, config.read_timeout) == (3, 10)
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}


def test_the_caller_policy_grants_the_reads_on_the_cluster():
    from fluid_build.iac.providers import aws_identity

    actions = aws_identity.caller_actions("emr-ec2")
    for name in ("DescribeCluster", "ListInstanceFleets", "ListInstanceGroups"):
        assert f"elasticmapreduce:{name}" in actions
    (statement, *_) = aws_identity.caller_document(
        [aws_identity.CallerRun("X", "emr-ec2", "j-ABC", "arn:role", ())],
        account="123456789012",
        region="eu-west-1",
    )["statement"]
    assert "elasticmapreduce:DescribeCluster" in statement["actions"]


@aws
@pytest.mark.emulated
def test_moto_round_trip_of_a_group_cluster_with_spot_core_nodes(ec2):
    mock_aws = pytest.importorskip("moto").mock_aws
    with mock_aws():
        client = boto3.client("emr", region_name="eu-west-1")
        spot = {"Market": "SPOT", "BidPrice": "0.10", "InstanceType": "m5.xlarge"}
        primary = {"Market": "ON_DEMAND", "InstanceType": "m5.xlarge", "InstanceCount": 1}
        cluster = client.run_job_flow(
            Name="shared",
            ReleaseLabel="emr-7.12.0",
            JobFlowRole="EMR_EC2_DefaultRole",
            ServiceRole="EMR_DefaultRole",
            Instances={
                "KeepJobFlowAliveWhenNoSteps": True,
                "InstanceGroups": [
                    {"InstanceRole": "MASTER", **primary},
                    {"InstanceRole": "CORE", "InstanceCount": 2, **spot},
                    {"InstanceRole": "TASK", "InstanceCount": 2, **spot},
                ],
            },
            Configurations=yarn_site(**ON),
        )["JobFlowId"]
        found = po.cluster_findings(
            [variant(ec2, "emr-ec2", {"clusterId": cluster})],
            clients=lambda s, r: boto3.client(s, region_name=r),
        )
    assert [(f.code, f.cluster) for f in found] == [(CORE_SPOT, cluster)]


# --- the fluid plan and fluid apply hooks -----------------------------------------------


@pytest.fixture
def said(monkeypatch):
    lines = []
    monkeypatch.setattr("fluid_build.cli.console.warning", lines.append)
    return lines


def test_a_dry_run_and_other_runtimes_read_no_cluster(run, ec2, said):
    _apply.warn_cluster_posture([ec2], LOG, dry_run=True, clients=forbidden)
    _apply.warn_cluster_posture([run], LOG, dry_run=False, clients=forbidden)
    assert said == []


def test_each_finding_is_one_warning_and_one_log_event(ec2, said, monkeypatch, caplog):
    finding = po.Finding(PRIMARY_SPOT, "j-ABC", ("ingest-orders",), "it runs [spot].", "fix.")
    monkeypatch.setattr(po, "cluster_findings", lambda runs, clients=None: [finding])
    with caplog.at_level(logging.INFO, logger=LOG.name):
        _apply.warn_cluster_posture([ec2], LOG, dry_run=False)
    assert said == [finding.line().replace("[spot]", "\\[spot]")]
    (record,) = [r for r in caplog.records if r.getMessage() == "spark_cluster_posture"]
    assert record.spark["code"] == PRIMARY_SPOT


def test_a_posture_check_that_fails_never_fails_plan_or_apply(
    ec2, contract, said, monkeypatch, caplog
):
    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(po, "cluster_findings", broken)
    with caplog.at_level(logging.DEBUG, logger=LOG.name):
        _apply.warn_cluster_posture([ec2], LOG, dry_run=False)
        monkeypatch.setattr(_apply, "_desired", broken)
        _apply.plan_posture(_ec2_contract(contract), LOG)
        monkeypatch.setattr(_apply, "_has_spark", broken)
        _apply.plan_posture(contract, LOG)
    assert said == []
    assert "spark_cluster_posture_failed error=RuntimeError" in caplog.text
    assert caplog.text.count("spark_cluster_posture_skipped reason=uncompiled") == 2


def _ec2_contract(contract):
    spark_cfg(contract).update(runtime="emr-ec2", clusterId="j-ABC")
    return contract


@pytest.fixture
def spot_primary(emr, monkeypatch):
    """``cluster_findings`` against a cluster whose primary node runs on Spot."""
    client, stub = emr
    answer(stub, described(), [group("MASTER", "SPOT")])
    real = po.cluster_findings
    monkeypatch.setattr(
        po, "cluster_findings", lambda runs, clients=None: real(runs, lambda s, r: client)
    )
    return stub


@aws
def test_fluid_plan_warns_and_keeps_the_warning_out_of_plan_json(
    contract, tmp_path, monkeypatch, said, spot_primary
):
    from fluid_build.cli import plan as plan_cli

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(_ec2_contract(contract)), encoding="utf-8")
    out = tmp_path / "plan.json"
    args = argparse.Namespace(
        contract=str(path),
        env=None,
        out=str(out),
        verbose=False,
        validate_actions=False,
        estimate_cost=False,
        check_sovereignty=None,
        provider=None,
        project=None,
        region=None,
    )
    assert plan_cli.run(args, LOG) == 0
    assert [line.split(":")[0] for line in said] == [
        "EMR cluster j-ABC (Spark build ingest-orders)"
    ]
    # The same plan from a cluster with nothing to warn about: one plan.json.
    monkeypatch.setattr(po, "cluster_findings", lambda runs, clients=None: [])
    quiet = tmp_path / "quiet.json"
    assert plan_cli.run(argparse.Namespace(**{**vars(args), "out": str(quiet)}), LOG) == 0
    assert len(said) == 1
    plans = [json.loads(path.read_text(encoding="utf-8")) for path in (out, quiet)]
    for plan in plans:  # the time, and the digest that covers it
        del plan["generated_at"], plan["planDigest"]
    assert plans[0] == plans[1]


@aws
def test_fluid_apply_warns_before_tofu_runs(contract, tmp_path, monkeypatch, spot_primary):
    _assert_apply_order(contract, tmp_path, monkeypatch, dry_run=False, expected=["warn", "tofu"])


def test_a_dry_run_apply_reads_no_cluster(contract, tmp_path, monkeypatch):
    monkeypatch.setattr(po, "cluster_findings", forbidden)
    _assert_apply_order(contract, tmp_path, monkeypatch, dry_run=True, expected=["tofu"])


def _assert_apply_order(contract, tmp_path, monkeypatch, *, dry_run, expected):
    from unittest.mock import patch

    from fluid_build.cli import apply as apply_cli
    from tests.cli.test_apply_hooks_every_engine import _args, _opentofu_engine

    order = []
    monkeypatch.setattr("fluid_build.cli.console.warning", lambda line: order.append("warn"))
    monkeypatch.setattr(_apply, "reconcile_after_apply", lambda *a, **k: 0)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text("id: placeholder\n", encoding="utf-8")
    with (
        patch(
            "fluid_build.cli.apply.load_contract_with_overlay",
            return_value=_ec2_contract(contract),
        ),
        _opentofu_engine(),
        patch("fluid_build.cli.apply._run_apply_hooks", return_value=0),
        patch(
            "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu",
            side_effect=lambda *a, **k: order.append("tofu") or 0,
        ),
    ):
        assert apply_cli.run(_args(str(path), dry_run=dry_run), LOG) == 0
    assert order == expected
