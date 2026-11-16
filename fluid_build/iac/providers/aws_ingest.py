# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The OpenTofu half of a forge Spark build on AWS: what runs the job, never the run.

For each ``engine: spark`` build with ``parameters.spark`` (``fluid_build/spark``) on a
0.7.7 contract, this emits everything with a create/read/update/delete lifecycle:

- the artifacts, as ``aws_s3_object`` under ``<bucket>/_fluid/spark/<product>/<build>/``
  in the output's bucket: the runner (package data) and ``job.json``
  (:func:`~fluid_build.spark.compiler.compile_job_config`). Both are written as
  ``content``, so the module is the same on every machine and the plan shows a changed
  config. An MSK source's brokers are read at plan time
  (``data.aws_msk_bootstrap_brokers``; ``bootstrap_brokers_sasl_iam`` for MSK IAM) and
  fill the compiler's ``${fluid_msk_bootstrap_brokers}`` placeholder;
- a CloudWatch log group per build, encrypted with a KMS key per product that only
  CloudWatch Logs, and only for this product's groups, may use;
- the job's role (:mod:`aws_identity`), unless the build names ``executionRoleArn``;
- what the runtime needs:

  - ``emr-serverless``: with ``applicationId``, nothing, since the application is the
    domain's (decision D3). With ``packaging.containers.emrApplication: isolated``, one
    ``aws_emrserverless_application`` per product. It auto-stops after 15 idle minutes,
    has a capacity cap, and runs in the MSK cluster's subnets and security groups.
  - ``glue``: an ``aws_glue_job``, ``gluestreaming`` or ``glueetl``, Glue 5.0 by
    default, with ``--datalake-formats iceberg``, the runner as its script and
    ``--config`` pointing at ``job.json``. With an MSK source it also gets a ``NETWORK``
    connection in one of the cluster's subnets.
  - ``emr-ec2``: an existing ``clusterId``; ``emr-eks``: an existing
    ``virtualClusterId``. Their runs are submitted by the runtime lane.

Starting, restarting and stopping the job run is not here: it is imperative, and runs in
the forge build runner (the ``aws-spark-runtime`` lane). Nothing here uses
``local-exec``.

Contract text never reaches an OpenTofu expression unescaped: the job config is compiled
with a marker for the account and for the brokers, escaped as a whole, and only the
markers become interpolations (:func:`_expression`).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ...providers.aws.util import warehouse as _warehouse
from ..base import UnsupportedBindingError
from ..naming import TofuExpr, bounded_ident, safe_ident, tofu_ref
from ..packaging import ContainerDecision, resolve_packaging
from . import aws_identity

#: The data source every account reference reads (shared with :mod:`aws`).
CALLER = "data.aws_caller_identity.fluid_lf_caller.account_id"
#: The region of a binding that names none.
REGION_DATA = "fluid_region"
#: Where a build's artifacts live in its output's bucket.
ARTIFACT_ROOT = "_fluid/spark"
RUNNER_FILE = "forge_spark_runner.py"
CONFIG_FILE = "job.json"
#: Days a Spark log group keeps events.
LOG_RETENTION_DAYS = 30
#: The isolated application's caps (EMR Serverless application limits).
APPLICATION_MAX_CPU = "16 vCPU"
APPLICATION_MAX_MEMORY = "64 GB"
APPLICATION_IDLE_MINUTES = 15
DEFAULT_RELEASE = "emr-7.12.0"
DEFAULT_GLUE_VERSION = "5.0"
#: Spark's Kafka source names its consumer groups ``spark-kafka-source-...`` unless the
#: build sets groupIdPrefix.
SPARK_GROUP_PREFIX = "spark-kafka-source-"
_MSK_ARN = re.compile(
    r"arn:aws[a-z-]*:kafka:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):cluster/"
    r"(?P<name>[A-Za-z0-9_.-]{1,64})/(?P<uuid>[A-Za-z0-9-]{1,128})"
)


@dataclass
class SparkBlocks:
    """What the Spark builds add to the module: ``resource`` and ``data`` maps."""

    resources: Dict[str, Dict[str, Any]]
    data: Dict[str, Dict[str, Any]]


def _escape(text: str) -> str:
    return text.replace("${", "$${").replace("%{", "%%{")


def _markers(contract: Mapping[str, Any]) -> Tuple[str, str]:
    """Two strings that occur nowhere in the contract: the account and the brokers.

    The compiler builds the default bucket from the account it is given, so its
    occurrences in the job config can only come from that argument. Lower-case letters
    and digits, as a bucket name allows.
    """
    text = json.dumps(contract, sort_keys=True, default=str)
    seed = hashlib.sha256(text.encode("utf-8")).hexdigest()
    for attempt in range(100):
        digest = hashlib.sha256(f"{seed}:{attempt}".encode()).hexdigest()
        account, brokers = f"fluidacct{digest[:20]}", f"fluidbrokers{digest[20:40]}"
        if account not in text and brokers not in text:
            return account, brokers
    raise ValueError("could not pick a marker absent from the contract")  # pragma: no cover


@dataclass(frozen=True)
class _Ctx:
    cid: str
    product: str
    account_marker: str
    brokers_marker: str
    account: TofuExpr = tofu_ref(CALLER)

    def x(self, text: str) -> Any:
        """``text`` (contract-derived) with the account marker made the account
        interpolation; plain (escaped by the renderer) when there is none."""
        return _expression(text, {self.account_marker: str(self.account)})


def _expression(text: str, swaps: Mapping[str, str]) -> Any:
    if not any(marker in text for marker in swaps):
        return text
    out = _escape(text)
    for marker, expression in swaps.items():
        out = out.replace(marker, expression)
    return TofuExpr(out)


def _msk(arn: str, topic: str, group_prefix: Optional[str], where: str) -> aws_identity.MskSource:
    match = _MSK_ARN.fullmatch(arn)
    if match is None:
        raise UnsupportedBindingError(
            "spark-msk-arn",
            f"{where}: parameters.spark.source.mskClusterArn {arn!r} is not a full MSK "
            "cluster ARN (arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid>), so the "
            "job's topic and consumer-group ARNs cannot be derived from it.",
            ("Copy the cluster ARN from the MSK console or `aws kafka list-clusters-v2`.",),
        )
    return aws_identity.MskSource(
        arn=arn,
        region=match["region"],
        account=match["account"],
        name=match["name"],
        uuid=match["uuid"],
        topic=topic,
        group_prefix=group_prefix or SPARK_GROUP_PREFIX,
    )


def _split_s3(uri: str) -> Tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def spark_blocks(contract: Mapping[str, Any], cid: str) -> SparkBlocks:
    """The resources and data sources of the contract's forge Spark builds.

    Empty for a contract without one, which keeps every other emit byte-identical. The
    operator's policies run first: the build's role and ``sparkConf``
    (:func:`aws_identity.refuse_unless_identity_allowed`), then the compiler's broker,
    registry, secrets and contract-SQL policies.
    """
    from fluid_build.spark.compiler import spark_builds
    from fluid_build.streaming.compiler import managed_lane_enabled

    blocks = SparkBlocks({}, {})
    if not managed_lane_enabled(contract):
        return blocks
    builds = spark_builds(contract)
    if not builds:
        return blocks
    account_marker, brokers_marker = _markers(contract)
    ctx = _Ctx(cid, str(contract.get("id")), account_marker, brokers_marker)
    product_tags = {"managed_by": "fluid", "fluid_contract": cid}
    blocks.data.setdefault("aws_caller_identity", {})["fluid_lf_caller"] = {}
    applications: Dict[str, Any] = {}
    for build in builds:
        _emit_build(blocks, contract, build, ctx, product_tags, applications)
    _emit_log_key(blocks, ctx, product_tags)
    return blocks


def _region(blocks: SparkBlocks, sink_region: Optional[str]) -> Any:
    if sink_region:
        return sink_region
    blocks.data.setdefault("aws_region", {})[REGION_DATA] = {}
    return tofu_ref(f"data.aws_region.{REGION_DATA}.region")


def _log_group_name(product: str, build_id: str) -> str:
    return f"/aws/fluid/spark/{product}/{build_id}"


def _emit_build(
    blocks: SparkBlocks,
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    ctx: _Ctx,
    product_tags: Mapping[str, str],
    applications: Dict[str, Any],
) -> None:
    from fluid_build.spark import runner_source
    from fluid_build.spark.compiler import (
        MSK_BROKERS_PLACEHOLDER,
        compile_job_config,
        spark_parameters,
    )

    bid = str(build.get("id"))
    where = f"Spark build {bid}"
    aws_identity.refuse_unless_identity_allowed(ctx.product, build)
    job = compile_job_config(contract, build, account_ref=ctx.account_marker)
    cfg = spark_parameters(build) or {}
    runtime = cfg["runtime"]
    key = safe_ident(f"{ctx.cid}_{bid}_spark")
    tags = {**product_tags, "fluid_build": bid}
    config = copy.deepcopy(dict(job.config))
    sink = config["sink"]
    region = _region(blocks, sink.get("region"))
    database, table = sink["table"].split(".", 1)

    expose = next(
        e
        for e in contract.get("exposes") or []
        if isinstance(e, Mapping) and e.get("exposeId") == (build.get("outputs") or [None])[0]
    )
    loc = (expose.get("binding") or {}).get("location") or {}
    bucket, data_prefix = _warehouse.normalize_location(loc, account_ref=ctx.account_marker)
    artifacts = f"{ARTIFACT_ROOT}/{ctx.product}/{bid}/"
    bucket_ref = _bucket_ref(contract, expose, loc, bucket, ctx)

    # The job config, its brokers from the MSK data source when it names a cluster.
    source = config.get("source") or {}
    msk: Optional[aws_identity.MskSource] = None
    swaps = {ctx.account_marker: str(ctx.account)}
    arn = source.get("mskClusterArn")
    if arn:
        brokers_attr = (
            "bootstrap_brokers_sasl_iam" if source.get("auth") == "msk-iam" else "bootstrap_brokers"
        )
        blocks.data.setdefault("aws_msk_bootstrap_brokers", {})[key] = {"cluster_arn": arn}
        if source.get("bootstrapServers") == MSK_BROKERS_PLACEHOLDER:
            source["bootstrapServers"] = ctx.brokers_marker
            swaps[ctx.brokers_marker] = str(
                tofu_ref(f"data.aws_msk_bootstrap_brokers.{key}.{brokers_attr}")
            )
        if source.get("auth") == "msk-iam":
            msk = _msk(arn, source["topic"], source.get("groupIdPrefix"), where)
    elif source.get("auth") == "msk-iam" and not cfg.get("executionRoleArn"):
        raise UnsupportedBindingError(
            "spark-msk-iam-needs-cluster-arn",
            f"{where} reads its brokers by bootstrapServers with auth: msk-iam, so forge "
            "cannot tell which MSK cluster, topic and consumer groups to grant the role it "
            "creates; the job would fail to authenticate.",
            (
                "Name the cluster by parameters.spark.source.mskClusterArn instead.",
                "Or set parameters.spark.executionRoleArn to a role that can read it.",
            ),
        )
    text = json.dumps(config, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    job_content = _expression(text, swaps)

    # Artifacts.
    objects = blocks.resources.setdefault("aws_s3_object", {})
    objects[f"{key}_runner"] = {
        "bucket": bucket_ref,
        "key": artifacts + RUNNER_FILE,
        "content": runner_source(),
        "content_type": "text/x-python",
        "tags": tags,
    }
    objects[f"{key}_config"] = {
        "bucket": bucket_ref,
        "key": artifacts + CONFIG_FILE,
        "content": job_content,
        "content_type": "application/json",
        "tags": tags,
    }
    runner_uri = tofu_ref(f"aws_s3_object.{key}_runner.bucket")
    script = TofuExpr(f"s3://{runner_uri}/{_escape(artifacts + RUNNER_FILE)}")
    config_uri = TofuExpr(
        f"s3://{tofu_ref(f'aws_s3_object.{key}_config.bucket')}/{_escape(artifacts + CONFIG_FILE)}"
    )

    # The log group.
    log_name = _log_group_name(ctx.product, bid)
    blocks.resources.setdefault("aws_cloudwatch_log_group", {})[key] = {
        "name": log_name,
        "retention_in_days": LOG_RETENTION_DAYS,
        "kms_key_id": tofu_ref(f"aws_kms_key.{safe_ident(f'{ctx.cid}_spark_logs')}.arn"),
        "tags": tags,
    }

    # The role.
    grants = _s3_grants(
        bucket, data_prefix, str(config.get("checkpoint") or ""), artifacts, runtime, ctx
    )
    secrets = _secret_arns(config, region, ctx)
    access = aws_identity.JobAccess(
        region=region,
        account=ctx.account,
        s3=grants,
        database=database,
        table=table,
        log_group_arn=tofu_ref(f"aws_cloudwatch_log_group.{key}.arn"),
        msk=msk,
        secret_arns=secrets,
    )
    role_arn: Any = cfg.get("executionRoleArn")
    role_name = bounded_ident(f"fluid-{ctx.product}-{bid}-spark", aws_identity.ROLE_NAME_LIMIT, "-")

    application_id: Any = None
    if runtime == "emr-serverless":
        application_id = _application(blocks, contract, expose, cfg, ctx, msk, applications, where)
    elif runtime == "emr-ec2" and not cfg.get("clusterId"):
        raise UnsupportedBindingError(
            "spark-emr-ec2-needs-cluster",
            f"{where} runs on EMR on EC2 without parameters.spark.clusterId; forge does not "
            "create transient EMR clusters yet.",
            ("Set parameters.spark.clusterId to the running cluster (j-...).",),
        )

    if role_arn is None:
        policy = f"${{data.aws_iam_policy_document.{key}_access.json}}"
        blocks.data.setdefault("aws_iam_policy_document", {})[f"{key}_access"] = (
            aws_identity.access_document(access)
        )
        if runtime == "emr-ec2":
            # A step runs as the cluster's instance profile, which the cluster owns:
            # forge gives its owner the job's least-privilege policy to attach.
            blocks.resources.setdefault("aws_iam_policy", {})[key] = {
                "name": role_name,
                "description": (
                    f"Least-privilege access of the forge Spark build {bid}; attach it to "
                    f"the EMR cluster's instance-profile role or a step runtime role"
                ),
                "policy": TofuExpr(policy),
                "tags": tags,
            }
        else:
            trust = _trust(blocks, runtime, cfg, ctx, key, region, role_name, application_id)
            blocks.data["aws_iam_policy_document"][f"{key}_trust"] = trust
            blocks.resources.setdefault("aws_iam_role", {})[key] = {
                "name": role_name,
                "description": f"The role the forge Spark build {bid} runs as",
                "assume_role_policy": tofu_ref(f"data.aws_iam_policy_document.{key}_trust.json"),
                "tags": tags,
            }
            blocks.resources.setdefault("aws_iam_role_policy", {})[key] = {
                "name": "fluid-spark-job",
                "role": tofu_ref(f"aws_iam_role.{key}.id"),
                "policy": TofuExpr(policy),
            }
            role_arn = tofu_ref(f"aws_iam_role.{key}.arn")

    if runtime == "glue":
        _glue_job(blocks, job.config["mode"], cfg, ctx, key, bid, tags, role_arn, script)
        args = blocks.resources["aws_glue_job"][key]["default_arguments"]
        args["--config"] = config_uri
        args["--continuous-log-logGroup"] = log_name
        args["--TempDir"] = TofuExpr(f"s3://{runner_uri}/{_escape(artifacts)}tmp/")
        if msk is not None:
            _glue_connection(blocks, ctx, key, bid, msk, tags)


def _bucket_ref(
    contract: Mapping[str, Any],
    expose: Mapping[str, Any],
    loc: Mapping[str, Any],
    bucket: str,
    ctx: _Ctx,
) -> Any:
    """The artifact bucket: the product's own bucket by reference (so the objects wait
    for it), else its name."""
    raw = loc.get("bucket")
    decision = resolve_packaging(contract).decision_for("bucket", expose.get("exposeId"))
    if raw and raw == bucket and decision is not ContainerDecision.REFERENCED:
        return tofu_ref(f"aws_s3_bucket.{safe_ident(f'{ctx.cid}_{raw}')}.id")
    return ctx.x(bucket)


def _s3_grants(
    bucket: str,
    data_prefix: str,
    checkpoint: str,
    artifacts: str,
    runtime: str,
    ctx: _Ctx,
) -> Tuple[aws_identity.S3Grant, ...]:
    by_bucket: Dict[str, Tuple[List[str], List[str]]] = {bucket: ([data_prefix], [artifacts])}
    if checkpoint:
        ck_bucket, ck_prefix = _split_s3(checkpoint)
        by_bucket.setdefault(ck_bucket, ([], []))[0].append(ck_prefix)
    if runtime == "glue":
        by_bucket[bucket][0].append(artifacts + "tmp/")
    return tuple(
        aws_identity.S3Grant(
            bucket=ctx.x(name),
            write=tuple(ctx.x(p) for p in dict.fromkeys(write)),
            read=tuple(ctx.x(p) for p in dict.fromkeys(read)),
        )
        for name, (write, read) in by_bucket.items()
    )


def _secret_arns(config: Mapping[str, Any], region: Any, ctx: _Ctx) -> Tuple[Any, ...]:
    """The Secrets Manager secrets the job reads: the mask key and a registry login."""
    arns: List[Any] = []
    mask = config.get("maskKey") or {}
    if mask.get("secretArn"):
        arns.append(mask["secretArn"])
    registry = (config.get("source") or {}).get("registry") or {}
    ref = (registry.get("auth") or {}).get("secretRef")
    if isinstance(ref, str) and ref.startswith("aws-sm://"):
        from fluid_build.secrets.ref import parse_secret_ref

        locator = parse_secret_ref(ref).locator
        if locator.startswith("arn:"):
            arns.append(locator)
        else:
            arns.append(
                aws_identity._join(
                    "arn:aws:secretsmanager:", region, ":", ctx.account, ":secret:", locator
                )
            )
    return tuple(dict.fromkeys(arns))


def _application(
    blocks: SparkBlocks,
    contract: Mapping[str, Any],
    expose: Mapping[str, Any],
    cfg: Mapping[str, Any],
    ctx: _Ctx,
    msk: Optional[aws_identity.MskSource],
    applications: Dict[str, Any],
    where: str,
) -> Any:
    """The EMR Serverless application id the job runs in, emitting it when isolated."""
    if cfg.get("applicationId"):
        return cfg["applicationId"]
    decision = resolve_packaging(contract).decision_for("emrApplication", expose.get("exposeId"))
    if decision is not ContainerDecision.OWNED:
        raise UnsupportedBindingError(
            "spark-emr-application-shared",
            f"{where} runs on EMR Serverless and names no parameters.spark.applicationId. "
            "The application is shared by the product's domain and owned by the platform "
            "(decision D3), so a product state does not create it.",
            (
                "Set parameters.spark.applicationId to the domain's EMR Serverless " "application.",
                "Or own one for this product: packaging.containers.emrApplication: isolated.",
            ),
        )
    release = cfg.get("releaseLabel") or DEFAULT_RELEASE
    app_key = safe_ident(f"{ctx.cid}_spark")
    if app_key in applications:
        first_release, first_cluster = applications[app_key]
        if release != first_release or (msk and first_cluster and msk.arn != first_cluster):
            raise UnsupportedBindingError(
                "spark-emr-application-conflict",
                f"{where} asks for the product's isolated EMR Serverless application with a "
                "different releaseLabel or MSK cluster than another build of the product; "
                "one application has one release and one network.",
                ("Give every isolated EMR Serverless build the same releaseLabel and cluster.",),
            )
        return tofu_ref(f"aws_emrserverless_application.{app_key}.id")
    applications[app_key] = (release, msk.arn if msk else None)
    body: Dict[str, Any] = {
        "name": bounded_ident(f"fluid-{ctx.product}", 64, "-"),
        "release_label": release,
        "type": "spark",
        "auto_stop_configuration": {
            "enabled": True,
            "idle_timeout_minutes": APPLICATION_IDLE_MINUTES,
        },
        "maximum_capacity": {"cpu": APPLICATION_MAX_CPU, "memory": APPLICATION_MAX_MEMORY},
        "tags": {"managed_by": "fluid", "fluid_contract": ctx.cid},
    }
    if msk is not None:
        cluster = _msk_cluster_data(blocks, app_key, msk)
        body["network_configuration"] = {
            "subnet_ids": tofu_ref(f"{cluster}.broker_node_group_info[0].client_subnets"),
            "security_group_ids": tofu_ref(f"{cluster}.broker_node_group_info[0].security_groups"),
        }
    blocks.resources.setdefault("aws_emrserverless_application", {})[app_key] = body
    return tofu_ref(f"aws_emrserverless_application.{app_key}.id")


def _msk_cluster_data(blocks: SparkBlocks, key: str, msk: aws_identity.MskSource) -> str:
    blocks.data.setdefault("aws_msk_cluster", {})[key] = {"cluster_name": msk.name}
    return f"data.aws_msk_cluster.{key}"


def _trust(
    blocks: SparkBlocks,
    runtime: str,
    cfg: Mapping[str, Any],
    ctx: _Ctx,
    key: str,
    region: Any,
    role_name: str,
    application_id: Any,
) -> Dict[str, Any]:
    issuer = namespace = None
    if runtime == "emr-eks":
        blocks.data.setdefault("aws_emrcontainers_virtual_cluster", {})[key] = {
            "virtual_cluster_id": cfg["virtualClusterId"]
        }
        vc = f"data.aws_emrcontainers_virtual_cluster.{key}.container_provider[0]"
        blocks.data.setdefault("aws_eks_cluster", {})[key] = {"name": tofu_ref(f"{vc}.id")}
        issuer = tofu_ref(
            f'trimprefix(data.aws_eks_cluster.{key}.identity[0].oidc[0].issuer, "https://")'
        )
        namespace = tofu_ref(f"{vc}.info[0].eks_info[0].namespace")
    return aws_identity.trust_document(
        runtime,
        account=ctx.account,
        region=region,
        role_name=role_name,
        application_id=application_id,
        eks_issuer=issuer,
        eks_namespace=namespace,
    )


def _glue_job(
    blocks: SparkBlocks,
    mode: str,
    cfg: Mapping[str, Any],
    ctx: _Ctx,
    key: str,
    bid: str,
    tags: Mapping[str, str],
    role_arn: Any,
    script: Any,
) -> None:
    streaming = mode == "streaming"
    blocks.resources.setdefault("aws_glue_job", {})[key] = {
        "name": bounded_ident(f"fluid-{ctx.product}-{bid}", 255, "-"),
        "description": f"The forge Spark build {bid}",
        "role_arn": role_arn,
        "glue_version": cfg.get("glueVersion") or DEFAULT_GLUE_VERSION,
        "command": {
            "name": "gluestreaming" if streaming else "glueetl",
            "script_location": script,
            "python_version": "3",
        },
        "default_arguments": {
            "--datalake-formats": "iceberg",
            "--enable-continuous-cloudwatch-log": "true",
            "--job-language": "python",
        },
        "worker_type": "G.1X",
        "number_of_workers": 2,
        "max_retries": 0,
        # One run at a time: a second run would share the checkpoint.
        "execution_property": {"max_concurrent_runs": 1},
        "tags": dict(tags),
    }


def _glue_connection(
    blocks: SparkBlocks,
    ctx: _Ctx,
    key: str,
    bid: str,
    msk: aws_identity.MskSource,
    tags: Mapping[str, str],
) -> None:
    """A Glue ``NETWORK`` connection in one of the MSK cluster's subnets, with its
    security groups: what puts a Glue job's workers where the brokers are."""
    cluster = _msk_cluster_data(blocks, key, msk)
    blocks.data.setdefault("aws_subnet", {})[key] = {
        "id": tofu_ref(f"tolist({cluster}.broker_node_group_info[0].client_subnets)[0]")
    }
    from . import aws as _aws  # imported here: aws imports this module

    name = bounded_ident(f"fluid-{ctx.product}-{bid}-msk", 255, "-")
    blocks.resources.setdefault("aws_glue_connection", {})[key] = {
        "name": name,
        "connection_type": "NETWORK",
        **_aws._emulator_catalog_id(),
        "physical_connection_requirements": {
            "availability_zone": tofu_ref(f"data.aws_subnet.{key}.availability_zone"),
            "security_group_id_list": tofu_ref(
                f"tolist({cluster}.broker_node_group_info[0].security_groups)"
            ),
            "subnet_id": tofu_ref(f"data.aws_subnet.{key}.id"),
        },
        "tags": dict(tags),
    }
    blocks.resources["aws_glue_job"][key]["connections"] = [
        tofu_ref(f"aws_glue_connection.{key}.name")
    ]


def _emit_log_key(blocks: SparkBlocks, ctx: _Ctx, tags: Mapping[str, str]) -> None:
    """The product's key for its Spark log groups. Only CloudWatch Logs may use it, and
    only for this product's ``/aws/fluid/spark/<product>/`` groups (CloudWatch Logs
    user guide, *Encrypt log data using AWS KMS*)."""
    key = safe_ident(f"{ctx.cid}_spark_logs")
    # The key and its log groups live in the provider's region.
    region_expr = _region(blocks, None)
    logs_arn = aws_identity._join(
        "arn:aws:logs:",
        region_expr,
        ":",
        ctx.account,
        ":log-group:",
        f"/aws/fluid/spark/{ctx.product}/*",
    )
    blocks.data.setdefault("aws_iam_policy_document", {})[f"{key}_key"] = {
        "statement": [
            {
                "sid": "EnableIamPolicies",
                "effect": "Allow",
                "principals": [
                    {"type": "AWS", "identifiers": [TofuExpr(f"arn:aws:iam::{ctx.account}:root")]}
                ],
                "actions": ["kms:*"],
                "resources": ["*"],
            },
            {
                "sid": "CloudWatchLogs",
                "effect": "Allow",
                "principals": [
                    {
                        "type": "Service",
                        "identifiers": [TofuExpr(f"logs.{region_expr}.amazonaws.com")],
                    }
                ],
                "actions": [
                    "kms:Decrypt*",
                    "kms:Describe*",
                    "kms:Encrypt*",
                    "kms:GenerateDataKey*",
                    "kms:ReEncrypt*",
                ],
                "resources": ["*"],
                "condition": [
                    {
                        "test": "ArnLike",
                        "variable": "kms:EncryptionContext:aws:logs:arn",
                        "values": [logs_arn],
                    }
                ],
            },
        ]
    }
    blocks.resources.setdefault("aws_kms_key", {})[key] = {
        "description": f"forge Spark log groups of {ctx.product}",
        "enable_key_rotation": True,
        "deletion_window_in_days": 30,
        "policy": tofu_ref(f"data.aws_iam_policy_document.{key}_key.json"),
        "tags": dict(tags),
    }


def merge(into: Dict[str, Dict[str, Any]], blocks: Mapping[str, Mapping[str, Any]]) -> None:
    """Add ``blocks`` to a ``resource`` or ``data`` map, refusing an address two emitters
    claim with different bodies."""
    for kind, bodies in blocks.items():
        target = into.setdefault(kind, {})
        for name, body in bodies.items():
            if name in target and target[name] != body:
                raise UnsupportedBindingError(
                    "spark-address-conflict",
                    f"{kind}.{name} is emitted twice with different bodies.",
                    ("Rename the build or the expose so their addresses differ.",),
                )
            target[name] = body
