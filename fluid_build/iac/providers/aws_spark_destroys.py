# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Before ``tofu apply`` destroys what a forge Spark job runs with: stop the job.

A plan that takes a Spark build away (the build left the contract, or the product went)
destroys the build's role, artifacts and log group, but not the run going on with them: a
stream on EMR Serverless, EMR on EKS or Glue, or a step on an EMR cluster, would keep going
until its credentials or files are gone, a writer nothing reconciles any more.
:meth:`AwsIacPlugin.prepare_destroys` hands :func:`prepare` the plan's deletes
(``runner.planned_deletes``: each resource's address, type and state before), and before the
apply:

1. An IAM managed policy the plan destroys must be attached to nothing. forge emits two, an
   EMR on EC2 build's job policy (for the cluster's owner to attach to its instance-profile
   role) and the product's caller policy (for the scheduler's role), and IAM refuses to
   delete an attached policy (``DeleteConflict``) part way through the apply. One that is
   attached refuses the apply, with the commands that detach it; nothing is stopped or
   destroyed.
2. Each Spark build whose log group goes has its active runs cancelled and waited for. Its
   runs are found by the build's run name where the log group's tags (``fluid_runtime``,
   ``fluid_run_target``, ``fluid_run_region``, ``aws_ingest``) say they run.
3. A Glue job that goes has its active runs stopped too: a build moved off Glue keeps its
   log group.
4. The product's own EMR Serverless application that goes has every active run in it
   cancelled, and is then stopped (``build_runners/spark/_stop.py``).

With ``dry_run`` the policies and the runs are read and nothing changes. A run still going
after the wait (``FLUID_SPARK_WAIT_SECONDS``), or an AWS error, refuses the apply before
anything is destroyed. The Databricks plugin's ``prepare_destroys`` is the same hook.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ...spark import names
from ..base import UnsupportedBindingError

Say = Callable[[str], None]
#: ``(service, region) -> boto3 client``, as ``build_runners/spark/_adapters.py`` takes it.
ClientFactory = Callable[[str, str], Any]

#: ``names.log_group_name``'s prefix: the log group of a forge Spark build.
_LOG_GROUPS = "/aws/fluid/spark/"
#: What ``ListEntitiesForPolicy`` returns a policy is attached to: field, kind, name key.
_HOLDERS = (
    ("PolicyRoles", "role", "RoleName"),
    ("PolicyUsers", "user", "UserName"),
    ("PolicyGroups", "group", "GroupName"),
)


def _tags(before: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("tags", "tags_all"):
        tags = before.get(key)
        if isinstance(tags, Mapping) and tags:
            return tags
    return {}


def _region(before: Mapping[str, Any]) -> Optional[str]:
    """A destroyed resource's region: the AWS provider's ``region`` attribute, else its ARN's."""
    if before.get("region"):
        return str(before["region"])
    parts = str(before.get("arn") or "").split(":")
    return parts[3] if len(parts) > 4 and parts[3] else None


def _iam_region(arn: str) -> str:
    """A region whose IAM endpoint serves ``arn``'s partition (IAM itself is global)."""
    partition = arn.split(":")[1] if arn.count(":") > 1 else "aws"
    return {"aws-cn": "cn-north-1", "aws-us-gov": "us-gov-west-1"}.get(partition, "us-east-1")


class _Destroys:
    """What the plan's deletes hold that this hook acts on."""

    def __init__(self) -> None:
        self.runs: Dict[Tuple[Any, ...], Any] = {}  # DesiredRun, by where its runs are
        self.applications: List[Tuple[str, Any]] = []  # (name, DesiredRun)
        self.policies: List[Tuple[str, str, bool]] = []  # (name, arn, a build's)
        self.untagged: Dict[Tuple[str, str], None] = {}  # (product, build), in order

    def add_run(self, run: Any) -> None:
        where = (run.runtime, run.region, tuple(sorted(run.target.items())), run.run_name)
        self.runs.setdefault(where, run)

    def build_runs(self) -> List[Any]:
        """The builds' runs, but those in an application that goes: it is stopped whole."""
        apps = {(r.region, r.target["applicationId"]) for _, r in self.applications}
        return [
            r
            for r in self.runs.values()
            if r.runtime != "emr-serverless"
            or (r.region, r.target.get("applicationId")) not in apps
        ]


def _tagged_run(
    found: _Destroys, product: str, build: str, tags: Mapping[str, Any], region: Any
) -> None:
    """The run a build's identity tags say it has; the build is ``untagged`` when they are
    missing (written by an earlier forge)."""
    from fluid_build.build_runners.spark._desired import located_run

    runtime = str(tags.get(names.RUNTIME_TAG) or "")
    key, _, ident = str(tags.get(names.RUN_TARGET_TAG) or "").partition("=")
    where = tags.get(names.RUN_REGION_TAG) or region
    if not (runtime and key and ident and where):
        found.untagged[(product, build)] = None
        return
    found.add_run(located_run(product, build, runtime, str(where), {key: ident}))


def _read(destroyed: Iterable[Mapping[str, Any]]) -> _Destroys:
    from fluid_build.build_runners.spark._desired import located_run

    found = _Destroys()
    for entry in destroyed:
        kind, before = entry.get("type"), entry.get("before") or {}
        tags = _tags(before)
        if tags.get("managed_by") != "fluid":
            continue
        product, build = tags.get(names.PRODUCT_TAG), tags.get(names.BUILD_TAG)
        region = _region(before)
        if kind == "aws_iam_policy" and before.get("arn"):
            found.policies.append((str(before.get("name") or ""), str(before["arn"]), bool(build)))
            if product and build:
                # An EMR on EC2 build's job policy goes with the build, or when the build
                # moves to another runtime and keeps its log group: its steps stop too.
                _tagged_run(found, str(product), str(build), tags, None)
        elif not (product and region):
            continue
        elif kind == "aws_cloudwatch_log_group" and build:
            if str(before.get("name") or "").startswith(_LOG_GROUPS):
                _tagged_run(found, str(product), str(build), tags, region)
        elif kind == "aws_glue_job" and build and before.get("name"):
            target = {"glueJobName": str(before["name"])}
            found.add_run(located_run(str(product), str(build), "glue", region, target))
        elif kind == "aws_emrserverless_application" and before.get("id"):
            target = {"applicationId": str(before["id"])}
            run = located_run(str(product), "", "emr-serverless", region, target)
            found.applications.append((str(before.get("name") or before["id"]), run))
    return found


def _holders(iam: Any, arn: str) -> List[Tuple[str, str]]:
    """``(kind, name)`` of each role, user and group ``arn`` is attached to."""
    held: List[Tuple[str, str]] = []
    for page in iam.get_paginator("list_entities_for_policy").paginate(PolicyArn=arn):
        for field, kind, key in _HOLDERS:
            held += [(kind, str(e.get(key))) for e in page.get(field) or []]
    return held


def _code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, Mapping) else None
    return str(error.get("Code") or "") if isinstance(error, Mapping) else ""


def _refuse_attached(
    policies: List[Tuple[str, str, bool]], clients: ClientFactory, say: Say
) -> None:
    attached = []
    for name, arn, of_build in policies:
        try:
            held = _holders(clients("iam", _iam_region(arn)), arn)
        except Exception as exc:  # noqa: BLE001 - the delete itself still says DeleteConflict
            if _code(exc) == "NoSuchEntity":
                continue
            say(
                f"  note: could not tell whether IAM policy {name} is attached "
                f"({_code(exc) or type(exc).__name__}); IAM deletes no attached policy, and "
                "the apply would then stop part way through the destroys."
            )
            continue
        if held:
            attached.append((name, arn, of_build, held))
    if not attached:
        return
    lines, fixes = [], []
    for name, arn, of_build, held in attached:
        whose = (
            "an EMR on EC2 build's job policy, which the cluster's owner attaches to its "
            "instance-profile role"
            if of_build
            else "the product's Spark caller policy, which the platform attaches to the "
            "scheduler's role"
        )
        lines.append(
            f"IAM policy {name} ({whose}) is attached to "
            + ", ".join(f"{kind} {who}" for kind, who in held)
        )
        fixes += [
            f"aws iam detach-{kind}-policy --{kind}-name {who} --policy-arn {arn}"
            for kind, who in held
        ]
    raise UnsupportedBindingError(
        "spark-policy-attached",
        "; ".join(lines) + ". The plan destroys it, and IAM deletes no attached policy "
        "(DeleteConflict), so the apply would stop part way through the destroys. Nothing was "
        "stopped or destroyed.",
        ("Detach it, then run fluid apply again:", *fixes),
    )


def prepare(
    destroyed: Iterable[Mapping[str, Any]],
    say: Say,
    *,
    dry_run: bool = False,
    clients: Optional[ClientFactory] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Refuse an attached policy the plan destroys, then stop the Spark runs its deletes
    would leave going (see the module docstring).

    Raises:
        UnsupportedBindingError: a policy is attached (``spark-policy-attached``), or a run
            could not be stopped (``spark-destroy-runs-going``): nothing was destroyed.
    """
    found = _read(destroyed)
    if not (found.runs or found.applications or found.policies or found.untagged):
        return
    from fluid_build.build_runners.spark import _stop
    from fluid_build.build_runners.spark._adapters import default_clients

    clients = clients or default_clients
    _refuse_attached(found.policies, clients, say)
    located = {(r.product, r.build) for r in found.runs.values()}
    for product, build in found.untagged:
        if (product, build) in located:
            continue  # another of its resources says where it runs
        say(
            f"  note: Spark build {build} goes, but nothing it is destroyed with says where it "
            "runs (an apply by an earlier forge): stop its run "
            f"{names.run_name(product, build)} yourself if one is going."
        )
    timing: Dict[str, Any] = {"sleep": sleep, "now": now, "env": env}
    failed: List[str] = []
    for out in _stop.stop_runs(found.build_runs(), clients=clients, dry_run=dry_run, **timing):
        if out.error:
            failed.append(f"Spark {out.build} ({out.runtime}): {out.message}")
        elif out.cancelled or out.action == "planned":
            say(f"  {out.line()}")
    for name, app in found.applications:
        out = _stop.stop_application(app, clients=clients, dry_run=dry_run, **timing)
        label = f"EMR Serverless application {name} ({app.target['applicationId']})"
        if out.error:
            failed.append(f"{label}: {out.message}")
        elif out.message:
            say(f"  {label}: {out.message}.")
    if not failed:
        return
    text = "; ".join(failed)
    if dry_run:
        say(f"  note: the apply would stop the runs first, and could not read them: {text}.")
        return
    raise UnsupportedBindingError(
        "spark-destroy-runs-going",
        f"{text}. The plan destroys what these runs run with, so nothing was destroyed.",
        (
            "Run fluid apply again once the runs have stopped, or stop them yourself.",
            "The identity running fluid apply needs the runtime's list and cancel actions "
            "(as for starting runs).",
        ),
    )
