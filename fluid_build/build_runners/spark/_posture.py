# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Whether an EMR on EC2 cluster can lose a Spark build's driver or application master to Spot.

``fluid plan`` and ``fluid apply`` read the cluster of each ``runtime: emr-ec2`` build once:
``DescribeCluster``, then ``ListInstanceGroups`` or ``ListInstanceFleets`` (the cluster's
``InstanceCollectionType``). Each finding is a warning that names its fix; none refuses:

| finding | when |
|---|---|
| ``primary-spot`` | the primary node runs on Spot, and the build's driver runs there (client mode) |
| ``core-spot`` | core nodes run on Spot: their HDFS blocks and containers go with an interruption |
| ``am-unpinned`` | core or task nodes run on Spot, and the yarn-site lets application masters use them |

An application master has to live as long as its job (Amazon EMR's reason for keeping masters
off Spot task nodes). YARN node labels keep application masters off Spot. Both settings are in ``yarn-site``:
``yarn.node-labels.enabled`` ``true``, and ``yarn.node-labels.am.default-node-label-expression``
``ON_DEMAND`` (EMR labels each node by its market from release 7.0, and managed scaling places
masters by that label from 7.2) or ``CORE`` (any release, safe while the core nodes are all
On-Demand). EMR 5.19 to 5.36 set ``CORE`` by default; 6.x and 7.x set no label. The yarn-site
the resource manager reads is the cluster's, overridden by the primary instance group's
applied configurations, or by those of the primary fleet's instance types. Sources: the Amazon
EMR Management Guide (node types; instance purchasing options; reconfiguring a running
cluster), the AWS Big Data Blog post on application master placement (EMR 7.2), and the EMR
API reference. No open-source checker covers this (Cloud Custodian's EMR filters, Prowler and
ScoutSuite have none).

A cluster that cannot be read (no boto3, no credentials, no permission, unreachable, ended)
gets a debug log line and nothing else. Every call is bounded (:data:`CLIENT_LIMITS`).
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build.build_runners.spark._desired import DesiredRun

LOG = logging.getLogger(__name__)

PRIMARY_SPOT, CORE_SPOT, AM_UNPINNED = "primary-spot", "core-spot", "am-unpinned"

ENABLED = "yarn.node-labels.enabled"
AM_LABEL = "yarn.node-labels.am.default-node-label-expression"
#: Groups and fleets that are gone or going: their market no longer matters.
_GONE = frozenset({"TERMINATING", "TERMINATED", "SHUTTING_DOWN", "ENDED"})
_CLUSTER_GONE = frozenset({"TERMINATING", "TERMINATED", "TERMINATED_WITH_ERRORS"})
#: ``botocore.config.Config`` arguments: a plan never waits long on an endpoint that does not
#: answer (seconds per connection and per read, two attempts per call).
CLIENT_LIMITS: Mapping[str, Any] = {
    "connect_timeout": 3,
    "read_timeout": 10,
    "retries": {"total_max_attempts": 2, "mode": "standard"},
}
_RELEASE = re.compile(r"emr-(\d+)\.(\d+)\.(\d+)")

Release = Optional[Tuple[int, int, int]]
Rule = Tuple[str, str, str]


@dataclasses.dataclass(frozen=True)
class Posture:
    """What the reads found: the node types with Spot capacity and the primary's yarn-site."""

    cluster: str
    fleets: bool
    release: Release
    spot: FrozenSet[str]
    #: None when the primary fleet's instance types set different yarn-site values.
    yarn_site: Optional[Mapping[str, str]]


@dataclasses.dataclass(frozen=True)
class Finding:
    code: str
    cluster: str
    builds: Tuple[str, ...]
    problem: str
    fix: str

    def line(self) -> str:
        noun = "build" if len(self.builds) == 1 else "builds"
        return (
            f"EMR cluster {self.cluster} (Spark {noun} {', '.join(self.builds)}): "
            f"{self.problem}\n    Fix: {self.fix}"
        )

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def posture_clients(service: str, region: str) -> Any:
    """The Spark adapters' client (``_adapters.default_clients``: the default credential
    chain, ``AWS_ENDPOINT_URL`` included), with every call bounded by :data:`CLIENT_LIMITS`.
    boto3 stays the adapters' one lazy import (``.importlinter-external``)."""
    from botocore.config import Config

    from fluid_build.build_runners.spark import _adapters

    return _adapters.default_clients(service, region, config=Config(**CLIENT_LIMITS))


def _release(label: Any) -> Release:
    found = _RELEASE.match(str(label or ""))
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def _yarn_site(configurations: Any) -> Dict[str, str]:
    """The ``yarn-site`` properties of an EMR configuration list (a later entry wins)."""
    found: Dict[str, str] = {}
    for item in configurations or []:
        if isinstance(item, Mapping) and item.get("Classification") == "yarn-site":
            found.update({str(k): str(v) for k, v in (item.get("Properties") or {}).items()})
    return found


def _state(item: Mapping[str, Any]) -> Any:
    return (item.get("Status") or {}).get("State")


def _listed(emr: Any, operation: str, key: str, cluster_id: str) -> Iterator[Mapping[str, Any]]:
    """Every group or fleet of the cluster that is not gone or going, all pages."""
    for page in emr.get_paginator(operation).paginate(ClusterId=cluster_id):
        for item in page.get(key) or []:
            if _state(item) not in _GONE:
                yield item


def _applied(group: Mapping[str, Any]) -> Any:
    """A group's configurations as applied: while a reconfiguration is pending, or after one
    failed, its last successfully applied ones."""
    wanted = group.get("ConfigurationsVersion")
    applied = group.get("LastSuccessfullyAppliedConfigurationsVersion")
    if wanted is not None and applied is not None and wanted != applied:
        return group.get("LastSuccessfullyAppliedConfigurations")
    return group.get("Configurations")


def _read_groups(emr: Any, cluster_id: str) -> Tuple[Set[str], Optional[Dict[str, str]]]:
    spot: Set[str] = set()
    primary: Dict[str, str] = {}
    for group in _listed(emr, "list_instance_groups", "InstanceGroups", cluster_id):
        if group.get("Market") == "SPOT":
            spot.add(str(group.get("InstanceGroupType")))
        if group.get("InstanceGroupType") == "MASTER":
            primary.update(_yarn_site(_applied(group)))
    return spot, primary


def _read_fleets(emr: Any, cluster_id: str) -> Tuple[Set[str], Optional[Dict[str, str]]]:
    spot: Set[str] = set()
    primary: Optional[Dict[str, str]] = {}
    for fleet in _listed(emr, "list_instance_fleets", "InstanceFleets", cluster_id):
        if fleet.get("TargetSpotCapacity") or fleet.get("ProvisionedSpotCapacity"):
            spot.add(str(fleet.get("InstanceFleetType")))
        if fleet.get("InstanceFleetType") == "MASTER":
            # The primary runs one of these instance types; the fleet does not say which.
            sites = [
                _yarn_site(spec.get("Configurations"))
                for spec in fleet.get("InstanceTypeSpecifications") or []
            ] or [{}]
            primary = sites[0] if all(site == sites[0] for site in sites) else None
    return spot, primary


def read_posture(cluster_id: str, region: str, clients: Any) -> Optional[Posture]:
    """The cluster's :class:`Posture`; None for a cluster that is ending or ended.

    Raises:
        Exception: whatever the client raises (no permission, unreachable, no credentials).
    """
    emr = clients("emr", region)
    cluster = emr.describe_cluster(ClusterId=cluster_id)["Cluster"]
    if _state(cluster) in _CLUSTER_GONE:
        LOG.debug("spark_cluster_posture_skipped cluster=%s state=%s", cluster_id, _state(cluster))
        return None
    fleets = cluster.get("InstanceCollectionType") == "INSTANCE_FLEET"
    spot, primary = (_read_fleets if fleets else _read_groups)(emr, cluster_id)
    return Posture(
        cluster=cluster_id,
        fleets=fleets,
        release=_release(cluster.get("ReleaseLabel")),
        spot=frozenset(spot),
        yarn_site=(
            None if primary is None else {**_yarn_site(cluster.get("Configurations")), **primary}
        ),
    )


def _label_defaults(release: Release) -> Tuple[str, str]:
    """``(enabled, expression)`` when the yarn-site sets neither: EMR 5.19 to 5.x keep
    application masters on CORE; earlier and later releases set no label."""
    if release is not None and (5, 19, 0) <= release < (6, 0, 0):
        return "true", "CORE"
    return "false", ""


def _shown(value: str) -> str:
    """A value read from the cluster, as printed: quoted when it holds a control character."""
    return value if value.isprintable() else repr(value)


def _am_risk(posture: Posture) -> Optional[str]:
    """Why application masters can land on Spot, or None when they cannot."""
    yarn = posture.yarn_site or {}
    default_on, default_label = _label_defaults(posture.release)
    enabled = yarn.get(ENABLED, default_on).strip()
    label = yarn.get(AM_LABEL, default_label).strip()
    if enabled.lower() != "true":
        return f"{ENABLED} is {_shown(enabled) if ENABLED in yarn and enabled else 'unset'}"
    if not label:
        return f"{AM_LABEL} is unset"
    if label == "ON_DEMAND":
        if posture.release is None or posture.release >= (7, 0, 0):
            return None
        return f"{AM_LABEL} is ON_DEMAND, a label EMR assigns from release 7.0 only"
    if label == "CORE":
        return f"{AM_LABEL} is CORE, and core nodes run on Spot" if "CORE" in posture.spot else None
    return f"{AM_LABEL} is {_shown(label)}, which forge does not know to be On-Demand"


def _primary_rule(posture: Posture) -> Optional[Rule]:
    if "MASTER" not in posture.spot:
        return None
    return (
        PRIMARY_SPOT,
        "the primary node runs on Spot. A Spot interruption ends the cluster, and the build's "
        "driver runs on that node (spark-submit --deploy-mode client).",
        "create the cluster with an On-Demand primary node; a running cluster's primary keeps "
        "its purchasing option.",
    )


def _core_rule(posture: Posture) -> Optional[Rule]:
    if "CORE" not in posture.spot:
        return None
    fix = (
        "move the core fleet to On-Demand capacity (TargetSpotCapacity 0, with aws emr "
        "modify-instance-fleet) and add Spot capacity as a task fleet."
        if posture.fleets
        else "create the cluster with an On-Demand core instance group and add Spot capacity "
        "as task instance groups; a core group keeps its purchasing option."
    )
    return (
        CORE_SPOT,
        "core nodes run on Spot. An interruption takes their HDFS blocks and the YARN "
        "containers they run.",
        fix,
    )


def _masters_rule(posture: Posture) -> Optional[Rule]:
    """Application masters never run on the primary node, so only Spot core or task nodes
    put them at risk."""
    if not posture.spot - {"MASTER"}:
        return None
    if posture.yarn_site is None:
        LOG.debug(
            "spark_cluster_posture_am_unknown cluster=%s reason=%s",
            posture.cluster,
            "the primary fleet's instance types set different yarn-site values",
        )
        return None
    why = _am_risk(posture)
    if why is None:
        return None
    release = posture.release
    label = "ON_DEMAND" if release is None or release >= (7, 2, 0) else "CORE"
    also = ", with On-Demand core nodes" if label == "CORE" else ""
    where = "fleet" if posture.fleets else "instance group"
    return (
        AM_UNPINNED,
        f"YARN can place the build's application master on a Spot node ({why}). A master "
        "has to live as long as its job, so a Spot interruption of that node interrupts the "
        "job.",
        f"set yarn-site {ENABLED}=true and {AM_LABEL}={label}{also}, when the cluster is "
        f"created or by reconfiguring its primary {where}.",
    )


def evaluate(posture: Posture) -> List[Rule]:
    """``(code, problem, fix)`` for each finding, primary first."""
    rules = (_primary_rule, _core_rule, _masters_rule)
    return [found for found in (rule(posture) for rule in rules) if found is not None]


def _unread(exc: BaseException) -> str:
    """A failed read's error code (botocore's, else the exception's class), never its text:
    an AccessDenied message carries the caller's ARN."""
    response = getattr(exc, "response", None)
    code = (response.get("Error") or {}).get("Code") if isinstance(response, Mapping) else None
    return str(code or type(exc).__name__)


def _clusters(runs: Sequence[DesiredRun]) -> Dict[Tuple[str, str], List[str]]:
    """``(region, clusterId) -> build ids`` of the EMR on EC2 runs whose region is known."""
    from fluid_build.build_runners.spark._desired import resolve_region

    builds: Dict[Tuple[str, str], List[str]] = {}
    for run in runs:
        cluster = run.target.get("clusterId") if run.runtime == "emr-ec2" else None
        if not cluster:
            continue
        try:
            region = resolve_region() if run.needs_region else run.region
        except Exception as exc:  # noqa: BLE001 - no region, or a profile botocore cannot read
            error = "NoRegion" if isinstance(exc, ValueError) else _unread(exc)
            LOG.debug("spark_cluster_posture_unread cluster=%s error=%s", cluster, error)
            continue
        builds.setdefault((region, str(cluster)), []).append(run.build)
    return builds


def cluster_findings(runs: Sequence[DesiredRun], clients: Any = None) -> List[Finding]:
    """The findings of each EMR on EC2 cluster ``runs`` start on, read once per cluster.

    Never raises: a cluster that cannot be read gets a debug log line and no finding.
    """
    found: List[Finding] = []
    for (region, cluster), names in _clusters(runs).items():
        try:
            posture = read_posture(cluster, region, clients or posture_clients)
        except Exception as exc:  # noqa: BLE001 - a warning-only read never fails plan or apply
            LOG.debug(
                "spark_cluster_posture_unread cluster=%s region=%s error=%s",
                cluster,
                region,
                _unread(exc),
            )
            continue
        if posture is not None:
            found += [Finding(c, cluster, tuple(names), p, f) for c, p, f in evaluate(posture)]
    return found
