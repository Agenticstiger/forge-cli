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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ...providers.aws.util import warehouse as _warehouse
from ...spark import names
from ..base import UnsupportedBindingError
from ..naming import TofuExpr, bounded_ident, safe_ident, tofu_ref
from ..packaging import ContainerDecision, resolve_packaging
from . import aws_glue_jars, aws_identity

#: The data source every account reference reads (shared with :mod:`aws`).
CALLER = "data.aws_caller_identity.fluid_lf_caller.account_id"
#: The region of a binding that names none.
REGION_DATA = "fluid_region"
#: Where a build's artifacts live in its output's bucket.
ARTIFACT_ROOT = names.ARTIFACT_PREFIX
#: Where the compiler puts a build's checkpoint by default: ``_checkpoints/<product>/<build>/``.
_CHECKPOINTS = "_checkpoints"
#: The roots forge writes in a product's bucket; an output's own data never lives there.
_RESERVED_ROOTS = (ARTIFACT_ROOT.split("/")[0], _CHECKPOINTS)
RUNNER_FILE = names.RUNNER_FILE
CONFIG_FILE = names.CONFIG_FILE
#: Days a Spark log group keeps events.
LOG_RETENTION_DAYS = 30
#: The isolated application's caps (EMR Serverless application limits).
APPLICATION_MAX_CPU = "16 vCPU"
APPLICATION_MAX_MEMORY = "64 GB"
APPLICATION_IDLE_MINUTES = 15
DEFAULT_RELEASE = names.DEFAULT_RELEASE
DEFAULT_GLUE_VERSION = "5.0"
#: Spark's Kafka source names its consumer groups ``spark-kafka-source-...`` unless the
#: build sets groupIdPrefix.
SPARK_GROUP_PREFIX = "spark-kafka-source-"


@dataclass(frozen=True)
class Writer:
    """A Spark job that writes an expose's table: who Lake Formation must let write it."""

    principal: Any  # the job role's ARN; None on emr-ec2 (the cluster's own role)
    overwrites: bool  # a batch job replaces what it computes


@dataclass
class SparkBlocks:
    """What the Spark builds add to the module: ``resource`` and ``data`` maps, and the
    jobs that write each expose (for :func:`fluid_build.iac.providers.aws`'s Lake
    Formation registration)."""

    resources: Dict[str, Dict[str, Any]]
    data: Dict[str, Dict[str, Any]]
    writers: Dict[str, List[Writer]] = field(default_factory=dict)
    #: expose id -> the key of the role Lake Formation registers its location with.
    registration_roles: Dict[str, str] = field(default_factory=dict)
    #: The ``aws_lakeformation_data_lake_settings`` data source that decides, at plan, whether
    #: a job role is granted its own table where the binding declares no lakeFormation block.
    lf_settings: Optional[str] = None


def _escape(text: str) -> str:
    return text.replace("${", "$${").replace("%{", "%%{")


def _plain_keys(value: Any) -> Any:
    """``value`` with every mapping key a string: YAML gives int and date keys too
    (``2024:``, ``2024-01-01:``), which ``json.dumps`` refuses as keys."""
    if isinstance(value, Mapping):
        return {str(k): _plain_keys(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_keys(v) for v in value]
    return value


def _markers(contract: Mapping[str, Any]) -> Tuple[str, str]:
    """Two strings that occur nowhere in the contract: the account and the brokers.

    The compiler builds the default bucket from the account it is given, so its
    occurrences in the job config can only come from that argument. Lower-case letters
    and digits, as a bucket name allows.
    """
    text = json.dumps(_plain_keys(contract), default=str)
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

    def shown(self, text: str) -> str:
        """``text`` for a message: the account marker as ``<account>``."""
        return text.replace(self.account_marker, "<account>")

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
    match = aws_identity.MSK_ARN.fullmatch(arn)
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
    from fluid_build.spark.compiler import LOCAL_RUNTIME, spark_builds, spark_parameters
    from fluid_build.streaming.compiler import managed_lane_enabled

    blocks = SparkBlocks({}, {})
    if not managed_lane_enabled(contract):
        return blocks
    # A build of runtime local runs on the machine fluid apply runs on: nothing on AWS.
    builds = [
        b
        for b in spark_builds(contract)
        if (spark_parameters(b) or {}).get("runtime") != LOCAL_RUNTIME
    ]
    if not builds:
        return blocks
    account_marker, brokers_marker = _markers(contract)
    ctx = _Ctx(cid, str(contract.get("id")), account_marker, brokers_marker)
    product_tags = {"managed_by": "fluid", "fluid_contract": cid}
    blocks.data.setdefault("aws_caller_identity", {})["fluid_lf_caller"] = {}
    applications: Dict[str, Any] = {}
    runs: List[aws_identity.CallerRun] = []
    keys: Dict[str, str] = {}
    for build in builds:
        bid = str(build.get("id"))
        key = safe_ident(f"{cid}_{bid}_spark")
        if key in keys:
            raise UnsupportedBindingError(
                "spark-build-id-clash",
                f"Spark builds {keys[key]!r} and {bid!r} differ only in '-' and '_', so their "
                "OpenTofu addresses meet and one build's role, artifacts and job would replace "
                "the other's.",
                ("Give the builds ids that differ in a letter or digit.",),
            )
        keys[key] = bid
        run = _emit_build(blocks, contract, build, ctx, product_tags, applications)
        if run is not None:
            runs.append(run)
    _emit_log_key(blocks, ctx, product_tags)
    if runs:
        _emit_caller_policy(blocks, ctx, runs, product_tags)
    for expose in contract.get("exposes") or []:
        if isinstance(expose, Mapping) and str(expose.get("exposeId")) in blocks.writers:
            _emit_registration_role(blocks, contract, ctx, expose, product_tags)
    _emit_lake_formation_settings(blocks, contract, cid)
    return blocks


def _lake_formation_default(binding: Mapping[str, Any]) -> bool:
    """A Glue table this binding names gets its writer's grant by default: it declares no
    ``governance.lakeFormation`` (one that does lists its own grants)."""
    from . import aws as _aws  # imported here: aws imports this module

    if ((binding.get("governance") or {}).get("lakeFormation")) or {}:
        return False
    return _aws._glue_cataloged(binding, binding.get("format") or "parquet")


def _emit_lake_formation_settings(
    blocks: SparkBlocks, contract: Mapping[str, Any], cid: str
) -> None:
    """The account's Lake Formation settings, read at plan, when a job role writes an output
    whose binding declares no ``governance.lakeFormation``: where Lake Formation governs new
    tables, the role is granted its table (``aws._emit_default_spark_writer_grants``), or it
    could not see it."""
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        writers = blocks.writers.get(str(expose.get("exposeId"))) or []
        if not _lake_formation_default(expose.get("binding") or {}):
            continue
        if any(w.principal is not None for w in writers):
            key = safe_ident(f"{cid}_lf_settings")
            blocks.data.setdefault("aws_lakeformation_data_lake_settings", {})[key] = {}
            blocks.lf_settings = key
            return


def _emit_registration_role(
    blocks: SparkBlocks,
    contract: Mapping[str, Any],
    ctx: _Ctx,
    expose: Mapping[str, Any],
    tags: Mapping[str, str],
) -> None:
    """The role Lake Formation registers a Spark-written location with.

    The service-linked role has no ``s3:PutObject`` on a registered location, and EMR 7.12
    writes Iceberg with the credentials Lake Formation vends: a location a forge Spark job
    writes is registered with this role instead, which may read and write the registered
    location (Lake Formation scopes what it vends to each table). One role per location,
    derived as :mod:`aws` registers it (``_lf_location``), so two exposes on one location
    share it. The location must be one named prefix: never a bucket root, nor a bucket a
    template could not name.
    """
    from . import aws as _aws  # imported here: aws imports this module

    binding = expose.get("binding") or {}
    gov = (binding.get("governance") or {}).get("lakeFormation") or {}
    if not (isinstance(gov, Mapping) and gov.get("registerLocation")):
        return
    loc = binding.get("location") or {}
    placement = _aws._placement(resolve_packaging(contract), expose)
    bucket, path = _aws._lf_location(loc, placement)
    if not bucket:
        return  # nothing registered (no location.bucket)
    expose_id = str(expose.get("exposeId"))
    where = f"exposes[{expose_id}].binding.location"
    if "${" in bucket or not path:
        # A template that did not resolve falls back to the account bucket, and a bucket
        # root may hold other teams' tables: a role that can write a whole bucket, or a
        # bucket forge cannot name, is refused rather than created.
        raise UnsupportedBindingError(
            "spark-lf-registration-scope",
            f"{where}: a location a forge Spark job writes is registered with a role that "
            "can write it, so it must be one named prefix: "
            + (
                "the bucket template did not resolve to a name."
                if "${" in bucket
                else "registerLocation without location.path would register, and let the "
                "role write, the whole bucket."
            ),
            (
                "Set location.path to the table's own prefix, and the bucket literally or "
                "through a template that resolves here.",
                "Or register the location outside forge (governance.lakeFormation without "
                "registerLocation).",
            ),
        )
    prefix = _checked_prefix(where, ctx, bucket, path, "the registered location", reserved=False)
    if _shared_bucket(contract, expose, loc):
        _refuse_unless_products_prefix(where, ctx, bucket, prefix)
    digest = hashlib.sha256(f"{bucket}/{prefix}".encode("utf-8")).hexdigest()[:8]
    key = safe_ident(f"{ctx.cid}_lf_writer_{digest}")
    blocks.registration_roles[expose_id] = key
    if key in blocks.resources.get("aws_iam_role", {}):
        return  # another expose on this location made it
    region = _region(blocks, loc.get("region"))
    documents = blocks.data.setdefault("aws_iam_policy_document", {})
    documents[f"{key}_trust"] = aws_identity.registration_trust(ctx.account)
    documents[f"{key}_access"] = aws_identity.registration_document(bucket, prefix, region=region)
    blocks.resources.setdefault("aws_iam_role", {})[key] = {
        "name": names.registration_role_name(ctx.product, digest),
        "description": (
            f"The role Lake Formation registers a location of {ctx.product} with, so its "
            "forge Spark jobs can write it"
        ),
        "assume_role_policy": tofu_ref(f"data.aws_iam_policy_document.{key}_trust.json"),
        "tags": dict(tags),
    }
    blocks.resources.setdefault("aws_iam_role_policy", {})[key] = {
        "name": "fluid-lakeformation-location",
        "role": tofu_ref(f"aws_iam_role.{key}.id"),
        "policy": tofu_ref(f"data.aws_iam_policy_document.{key}_access.json"),
    }


def _region(blocks: SparkBlocks, sink_region: Optional[str]) -> Any:
    if sink_region:
        return sink_region
    blocks.data.setdefault("aws_region", {})[REGION_DATA] = {}
    return tofu_ref(f"data.aws_region.{REGION_DATA}.region")


def _provider_region(blocks: SparkBlocks) -> TofuExpr:
    """The provider's region: where the application, the jobs and the log groups live."""
    blocks.data.setdefault("aws_region", {})[REGION_DATA] = {}
    return tofu_ref(f"data.aws_region.{REGION_DATA}.region")


@dataclass
class _Build:
    """One Spark build as the emitters below read it."""

    ctx: _Ctx
    build_id: str
    key: str
    cfg: Mapping[str, Any]
    config: Dict[str, Any]
    tags: Dict[str, str]
    region: Any
    artifacts: str
    runner_sha256: str

    @property
    def runner_key(self) -> str:
        return names.runner_key(self.ctx.product, self.build_id, self.runner_sha256)

    @property
    def runtime(self) -> str:
        return str(self.cfg["runtime"])

    @property
    def where(self) -> str:
        return f"Spark build {self.build_id}"

    def object_uri(self, key: str) -> TofuExpr:
        """``s3://`` of one artifact key, through the runner object so a reader waits for
        the upload."""
        bucket = tofu_ref(f"aws_s3_object.{self.key}_runner.bucket")
        return TofuExpr(f"s3://{bucket}/{_escape(key)}")


def _emit_build(
    blocks: SparkBlocks,
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    ctx: _Ctx,
    product_tags: Mapping[str, str],
    applications: Dict[str, Any],
) -> Optional[aws_identity.CallerRun]:
    """Emit one build; return what a scheduler needs to start its runs, for a build that
    runs to completion (batch, or a stream triggered availableNow)."""
    from fluid_build.spark import runner_source
    from fluid_build.spark.compiler import compile_job_config, spark_parameters

    bid = str(build.get("id"))
    problems = aws_identity.runtime_errors(build)
    if problems:
        raise UnsupportedBindingError(
            "spark-runtime-field",
            "; ".join(problems) + ".",
            ("Fix the field the message names; fluid validate reports the same.",),
        )
    aws_identity.refuse_unless_identity_allowed(ctx.product, build)
    job = compile_job_config(contract, build, account_ref=ctx.account_marker)
    config = copy.deepcopy(dict(job.config))
    b = _Build(
        ctx=ctx,
        build_id=bid,
        key=safe_ident(f"{ctx.cid}_{bid}_spark"),
        cfg=spark_parameters(build) or {},
        config=config,
        tags={**product_tags, **names.tags(ctx.product, bid)},
        region=_region(blocks, config["sink"].get("region")),
        artifacts=names.artifact_prefix(ctx.product, bid),
        runner_sha256=names.runner_sha256(runner_source()),
    )
    expose = next(
        e
        for e in contract.get("exposes") or []
        if isinstance(e, Mapping) and e.get("exposeId") == (build.get("outputs") or [None])[0]
    )
    loc = (expose.get("binding") or {}).get("location") or {}
    bucket, data_prefix = _warehouse.normalize_location(loc, account_ref=ctx.account_marker)

    content, cluster, iam = _job_content(blocks, b)
    bucket_ref = _bucket_ref(contract, expose, loc, bucket, ctx)
    _emit_artifacts(blocks, b, bucket_ref, content, runner_source())
    log_name = names.log_group_name(ctx.product, bid)
    blocks.resources.setdefault("aws_cloudwatch_log_group", {})[b.key] = {
        "name": log_name,
        "retention_in_days": LOG_RETENTION_DAYS,
        "kms_key_id": tofu_ref(f"aws_kms_key.{safe_ident(f'{ctx.cid}_spark_logs')}.arn"),
        "tags": b.tags,
    }

    application_id: Any = None
    if b.runtime == "emr-serverless":
        application_id = _application(blocks, contract, expose, b, cluster, applications)
    elif b.runtime == "emr-ec2" and not b.cfg.get("clusterId"):
        raise UnsupportedBindingError(
            "spark-emr-ec2-needs-cluster",
            f"{b.where} runs on EMR on EC2 without parameters.spark.clusterId; forge does not "
            "create transient EMR clusters yet.",
            ("Set parameters.spark.clusterId to the running cluster (j-...).",),
        )
    blocks.resources["aws_cloudwatch_log_group"][b.key]["tags"] = {
        **b.tags,
        **_run_identity(b, application_id),
    }
    database, table = config["sink"]["table"].split(".", 1)
    vpc = b.runtime == "glue" and cluster is not None
    jars = _glue_jars(blocks, b, bucket_ref) if b.runtime == "glue" else ()
    dead_table, dead_prefix, dead_topic = _dead_letters(b, contract, expose, data_prefix, iam)
    access = aws_identity.JobAccess(
        region=b.region,
        account=ctx.account,
        s3=_s3_grants(
            b,
            bucket,
            data_prefix,
            shared=_shared_bucket(contract, expose, loc),
            dead_letters=dead_prefix,
        ),
        database=database,
        table=table,
        log_group_arn=tofu_ref(f"aws_cloudwatch_log_group.{b.key}.arn"),
        msk=cluster if iam else None,
        secret_arns=_secret_arns(config, b.region, ctx),
        glue_vpc=vpc,
        glue_subnet_arn=tofu_ref(f"data.aws_subnet.{b.key}.arn") if vpc else None,
        dead_letter_table=dead_table,
        dead_letter_topic=dead_topic,
    )
    role_arn = _emit_role(blocks, b, access, application_id)
    if dead_table and dead_prefix:
        principal = None if b.runtime == "emr-ec2" else role_arn
        _emit_dead_letter_table(blocks, contract, expose, b, dead_table, dead_prefix, principal)
    overwrites = job.config["mode"] == "batch"
    # On emr-ec2 the cluster's own role writes, which forge does not know.
    principal = None if b.runtime == "emr-ec2" else role_arn
    blocks.writers.setdefault(str(expose.get("exposeId")), []).append(Writer(principal, overwrites))
    target: Any = application_id
    if b.runtime == "glue":
        _glue_job(blocks, b, job.config["mode"], role_arn)
        args = blocks.resources["aws_glue_job"][b.key]["default_arguments"]
        args["--config"] = b.object_uri(names.job_config_key(ctx.product, bid))
        args["--continuous-log-logGroup"] = log_name
        args["--TempDir"] = b.object_uri(b.artifacts + "tmp/")
        if jars:
            args["--extra-jars"] = TofuExpr(",".join(jars))
            # Glue 5's hudi-utilities bundle carries its own copy of the Confluent
            # registry client classes; ours must win (measured: with this flag the client
            # loads from our pinned jar). Only Avro builds add jars, so only they set it.
            args["--user-jars-first"] = "true"
        if vpc:
            _glue_connection(blocks, b, cluster)
        target = tofu_ref(f"aws_glue_job.{b.key}.name")
    elif b.runtime == "emr-ec2":
        target = b.cfg["clusterId"]
    elif b.runtime == "emr-eks":
        target = b.cfg["virtualClusterId"]
    if not (overwrites or (config.get("trigger") or {}).get("availableNow")):
        return None
    bucket_arn = aws_identity._join("arn:aws:s3:::", ctx.x(bucket))
    return aws_identity.CallerRun(
        sid=safe_ident(bid).replace("_", "").capitalize() + names.build_key(ctx.product, bid),
        runtime=b.runtime,
        target=target,
        role_arn=role_arn,
        objects=(
            aws_identity._join(bucket_arn, "/", names.job_config_key(ctx.product, bid)),
            aws_identity._join(bucket_arn, "/", b.runner_key),
        ),
        lookup_application=b.runtime == "emr-serverless" and not b.cfg.get("applicationId"),
    )


def _run_identity(b: _Build, application_id: Any) -> Dict[str, Any]:
    """Where the build's runs are, as tags on its log group and an EMR on EC2 build's job
    policy (:data:`names.RUNTIME_TAG` and the two beside it): a plan that destroys them no
    longer has the build in the contract, or not on that runtime, and reads them from the
    state to stop its runs first (``aws_spark_destroys``)."""
    key, ident = {
        "emr-serverless": ("applicationId", application_id),
        "glue": ("glueJobName", names.glue_job_name(b.ctx.product, b.build_id)),
        "emr-ec2": ("clusterId", b.cfg.get("clusterId")),
        "emr-eks": ("virtualClusterId", b.cfg.get("virtualClusterId")),
    }[b.runtime]
    return {
        names.RUNTIME_TAG: b.runtime,
        names.RUN_TARGET_TAG: aws_identity._join(f"{key}=", ident),
        names.RUN_REGION_TAG: b.region,
    }


def _glue_jars(blocks: SparkBlocks, b: _Build, bucket_ref: Any) -> Tuple[TofuExpr, ...]:
    """The pinned jars a Glue build adds (:mod:`aws_glue_jars`), as ``s3://`` URIs of the
    build's own verified copies.

    Each jar is read from the platform's prefix with its S3 checksum and copied into the
    build's content-addressed artifact prefix (``aws_glue_jars.copy_key``), where the job
    loads it from:

    - the copy's ``precondition``: the source's ``checksum_sha256`` is the pin, at plan;
    - ``copy_if_match`` is the ETag that plan read, so S3 copies those bytes or nothing;
    - the copy is written with a SHA-256 checksum, and its ``postcondition`` checks it.

    A later swap in the platform's prefix never reaches the job: the copy is not re-made
    while its pin and source are unchanged, and a new pin is a new key. The job role reads
    its own artifact prefix only, never the platform's.
    """
    needed = aws_glue_jars.needed_jars(b.config)
    if not needed:
        return ()
    try:
        location = aws_glue_jars.jars_location()
    except ValueError as exc:
        raise UnsupportedBindingError("spark-glue-jars-url", f"{b.where}: {exc}.", ()) from exc
    if location is None:
        raise UnsupportedBindingError(
            "spark-glue-jars-unset",
            f"{b.where} decodes Confluent Avro on Glue, which ships no ABRiS: the job loads "
            f"the pinned jars from the platform's prefix, and {aws_glue_jars.JARS_URL_ENV} "
            "names none.",
            (
                f"Have the operator set {aws_glue_jars.JARS_URL_ENV}=s3://<bucket>/<prefix>/ "
                "and upload the pinned jars there, each single-part with "
                "--checksum-algorithm SHA256 (aws_glue_jars.AVRO_JARS lists them).",
                "Or run the build on emr-serverless, which resolves its jars from Maven.",
            ),
        )
    uris = []
    sources = blocks.data.setdefault("aws_s3_object", {})
    copies = blocks.resources.setdefault("aws_s3_object_copy", {})
    for index, jar in enumerate(needed):
        name = f"{b.key}_jar_{index}"
        where = f"s3://{location.bucket}/{location.key(jar)}"
        sources[name] = {
            "bucket": location.bucket,
            "key": location.key(jar),
            "checksum_mode": "ENABLED",
        }
        key = aws_glue_jars.copy_key(b.ctx.product, b.build_id, jar)
        copies[name] = {
            "bucket": bucket_ref,
            "key": key,
            "source": f"{location.bucket}/{location.key(jar)}",
            "copy_if_match": tofu_ref(f"data.aws_s3_object.{name}.etag"),
            "checksum_algorithm": "SHA256",
            # The product's tags, not the platform object's (S3 copies a source's tags
            # unless told to replace them).
            "tagging_directive": "REPLACE",
            "tags": dict(b.tags),
            "lifecycle": {
                "precondition": [
                    {
                        "condition": TofuExpr(
                            f"${{data.aws_s3_object.{name}.checksum_sha256 == "
                            f'"{jar.sha256_b64}"}}'
                        ),
                        "error_message": (
                            f"{where} is not the pinned {jar.coordinate} (sha256 "
                            f"{jar.sha256}), or was uploaded without a single-part SHA-256 "
                            "checksum."
                        ),
                    }
                ],
                "postcondition": [
                    {
                        "condition": TofuExpr(f'${{self.checksum_sha256 == "{jar.sha256_b64}"}}'),
                        "error_message": (
                            f"The copy of {jar.coordinate} is not the pinned bytes (sha256 "
                            f"{jar.sha256})."
                        ),
                    }
                ],
            },
        }
        bucket = tofu_ref(f"aws_s3_object_copy.{name}.bucket")
        uris.append(TofuExpr(f"s3://{bucket}/{_escape(key)}"))
    return tuple(uris)


def _job_content(
    blocks: SparkBlocks, b: _Build
) -> Tuple[Any, Optional[aws_identity.MskSource], bool]:
    """``job.json``'s text with its brokers from the MSK data source when it names a
    cluster; that cluster (its network, and its topic and groups for the role); and
    whether the job reads it with MSK IAM."""
    from fluid_build.spark.compiler import MSK_BROKERS_PLACEHOLDER

    ctx, source = b.ctx, b.config.get("source") or {}
    swaps = {ctx.account_marker: str(ctx.account)}
    cluster: Optional[aws_identity.MskSource] = None
    arn = source.get("mskClusterArn")
    iam = source.get("auth") == "msk-iam"
    if arn:
        cluster = _msk(arn, source["topic"], source.get("groupIdPrefix"), b.where)
        # MSK IAM reads the SASL/IAM listener. Without IAM the job takes the TLS listener:
        # MSK encrypts client traffic by default, and its plaintext list is then empty (a
        # plaintext listener is named by bootstrapServers instead). The compiler marks such
        # a source ``tls`` for the runner; this only fills the brokers.
        attr = "bootstrap_brokers_sasl_iam" if iam else "bootstrap_brokers_tls"
        blocks.data.setdefault("aws_msk_bootstrap_brokers", {})[b.key] = {
            "cluster_arn": arn,
            "region": cluster.region,
        }
        if source.get("bootstrapServers") == MSK_BROKERS_PLACEHOLDER:
            source["bootstrapServers"] = ctx.brokers_marker
            swaps[ctx.brokers_marker] = str(
                tofu_ref(f"data.aws_msk_bootstrap_brokers.{b.key}.{attr}")
            )
    elif iam and not b.cfg.get("executionRoleArn"):
        raise UnsupportedBindingError(
            "spark-msk-iam-needs-cluster-arn",
            f"{b.where} reads its brokers by bootstrapServers with auth: msk-iam, so forge "
            "cannot tell which MSK cluster, topic and consumer groups to grant the role it "
            "creates; the job would fail to authenticate.",
            (
                "Name the cluster by parameters.spark.source.mskClusterArn instead.",
                "Or set parameters.spark.executionRoleArn to a role that can read it.",
            ),
        )
    text = json.dumps(b.config, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if "${" in text:
        # The runner refuses a config that still holds one, and OpenTofu would write the
        # contract's own literally: fail here, before anything is uploaded.
        raise UnsupportedBindingError(
            "spark-config-interpolation",
            f"{b.where}: the job config holds '${{' from the contract (in properties.sql or "
            "another field), which the forge Spark runner refuses as an unresolved "
            "interpolation.",
            ("Write the text without '${' (in SQL, a literal '$' then '{' works).",),
        )
    return _expression(text, swaps), cluster, iam


def _emit_artifacts(
    blocks: SparkBlocks, b: _Build, bucket_ref: Any, content: Any, runner: str
) -> None:
    objects = blocks.resources.setdefault("aws_s3_object", {})
    objects[f"{b.key}_runner"] = {
        "bucket": bucket_ref,
        "key": b.runner_key,
        "content": runner,
        "content_type": "text/x-python",
        "tags": b.tags,
    }
    objects[f"{b.key}_config"] = {
        "bucket": bucket_ref,
        "key": names.job_config_key(b.ctx.product, b.build_id),
        "content": content,
        "content_type": "application/json",
        "tags": b.tags,
    }


def _emit_role(
    blocks: SparkBlocks, b: _Build, access: aws_identity.JobAccess, application_id: Any
) -> Any:
    """The role the job runs as, and its ARN; a named ``executionRoleArn`` as it is."""
    if b.cfg.get("executionRoleArn"):
        return b.cfg["executionRoleArn"]
    name = names.execution_role_name(b.ctx.product, b.build_id)
    documents = blocks.data.setdefault("aws_iam_policy_document", {})
    documents[f"{b.key}_access"] = aws_identity.access_document(access)
    policy = tofu_ref(f"data.aws_iam_policy_document.{b.key}_access.json")
    if b.runtime == "emr-ec2":
        # A step runs as the cluster's instance profile, which the cluster owns:
        # forge gives its owner the job's least-privilege policy to attach.
        blocks.resources.setdefault("aws_iam_policy", {})[b.key] = {
            "name": name,
            "description": (
                f"Least-privilege access of the forge Spark build {b.build_id}; attach it "
                "to the EMR cluster's instance-profile role or a step runtime role"
            ),
            "policy": policy,
            # Its steps' cluster too: the policy goes when the build moves to another
            # runtime, while its log group stays (``aws_spark_destroys``).
            "tags": {**b.tags, **_run_identity(b, None)},
        }
        return None
    documents[f"{b.key}_trust"] = _trust(blocks, b, name, application_id)
    blocks.resources.setdefault("aws_iam_role", {})[b.key] = {
        "name": name,
        "description": f"The role the forge Spark build {b.build_id} runs as",
        "assume_role_policy": tofu_ref(f"data.aws_iam_policy_document.{b.key}_trust.json"),
        "tags": b.tags,
    }
    blocks.resources.setdefault("aws_iam_role_policy", {})[b.key] = {
        "name": "fluid-spark-job",
        "role": tofu_ref(f"aws_iam_role.{b.key}.id"),
        "policy": policy,
    }
    return tofu_ref(f"aws_iam_role.{b.key}.arn")


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


#: What a forge role's S3 grant may name: a bucket name and a prefix with no IAM
#: wildcard (``*``, ``?``), policy variable (``$``) or ``..`` segment.
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
#: The account's shared fallback bucket, named literally.
_ACCOUNT_BUCKET = re.compile(r"[0-9]{12}-fluid-data")
_PREFIX = re.compile(r"[A-Za-z0-9._=-]+(?:/[A-Za-z0-9._=-]+)*/")


def _scoped(b: _Build, bucket: str, prefix: str, what: str) -> str:
    return _checked_prefix(
        b.where, b.ctx, bucket, prefix, what, reserved=what == "properties.stream.checkpoint"
    )


def _checked_prefix(
    where: str, ctx: _Ctx, bucket: str, prefix: str, what: str, *, reserved: bool
) -> str:
    """``prefix`` ending in ``/``, refused unless it names one place in one bucket.

    A role forge creates can read, overwrite and delete what its prefixes match. An empty
    prefix is the whole bucket, which a bucketless product shares with every other
    (``<account>-fluid-data``); a wildcard or a prefix without its ``/`` reaches its
    neighbours (``x`` matches ``x_pii/``). ``reserved`` lets the prefix sit under forge's
    own ``_fluid/`` and ``_checkpoints/`` roots (a checkpoint does).
    """
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    segments = prefix.split("/")
    in_reserved = not reserved and segments[0] in _RESERVED_ROOTS
    if (
        _BUCKET.fullmatch(bucket)
        and _PREFIX.fullmatch(prefix)
        and ".." not in segments
        and "." not in segments
        and not in_reserved
    ):
        return prefix
    raise UnsupportedBindingError(
        "spark-role-scope",
        f"{where}: {what} (s3://{ctx.shown(bucket)}/{prefix.rstrip('/')}) is not one "
        "prefix of one bucket outside forge's _fluid/ and _checkpoints/ roots, and the "
        "role forge creates for it may read, overwrite and delete everything it names: "
        "an empty path is the whole bucket, and a bucketless product shares "
        "<account>-fluid-data with every other.",
        (
            "Set binding.location.path to the table's own prefix (letters, digits, "
            "'.', '_', '=', '-' and '/').",
            "Or set parameters.spark.executionRoleArn to a role the operator scoped.",
        ),
    )


def _shared_bucket(
    contract: Mapping[str, Any], expose: Mapping[str, Any], loc: Mapping[str, Any]
) -> bool:
    """Whether the output's bucket holds other products' data: the account's
    ``<account>-fluid-data`` fallback, or a platform pool (packaging ``shared``)."""
    if _warehouse.bucket_uses_fallback(loc) or _ACCOUNT_BUCKET.fullmatch(str(loc.get("bucket"))):
        return True
    decision = resolve_packaging(contract).decision_for("bucket", expose.get("exposeId"))
    return decision is ContainerDecision.REFERENCED


def _refuse_unless_products_prefix(where: str, ctx: _Ctx, bucket: str, prefix: str) -> None:
    """In a bucket other products share, a role forge creates writes the product's own
    ``<product>/`` prefix only: any other prefix could be another product's table."""
    if prefix.startswith(f"{ctx.product}/"):
        return
    raise UnsupportedBindingError(
        "spark-role-shared-prefix",
        f"{where}: s3://{ctx.shown(bucket)}/{prefix} is in a bucket other products share, "
        f"and the role forge creates may write only this product's own {ctx.product}/ "
        "prefix there; any other prefix could hold another product's table.",
        (
            f"Set binding.location.path under {ctx.product}/ (for example "
            f"{ctx.product}/<table>/).",
            "Or give the product its own bucket (binding.location.bucket, owned).",
            "Or set parameters.spark.executionRoleArn to a role the operator scoped.",
        ),
    )


def _s3_grants(
    b: _Build,
    bucket: str,
    data_prefix: str,
    *,
    shared: bool = False,
    dead_letters: Optional[str] = None,
) -> Tuple[aws_identity.S3Grant, ...]:
    """The prefixes the job writes (data, checkpoint, Glue's TempDir, the dead letters'
    table) and reads (artifacts).

    For a role forge creates, every prefix is checked to be one place (:func:`_scoped`),
    and a declared checkpoint must be the product's: under ``_checkpoints/<product>/`` in
    the output's bucket. A checkpoint elsewhere would give the role write access to a
    bucket or prefix the contract chose.
    """
    owned = not b.cfg.get("executionRoleArn")
    if owned:
        data_prefix = _scoped(b, bucket, data_prefix, "the output's location")
        if shared:
            _refuse_unless_products_prefix(b.where, b.ctx, bucket, data_prefix)
    by_bucket: Dict[str, Tuple[List[str], List[str]]] = {bucket: ([data_prefix], [b.artifacts])}
    if dead_letters is not None:
        by_bucket[bucket][0].append(_dead_letter_grant(b, bucket, dead_letters, owned, shared))
    checkpoint = str(b.config.get("checkpoint") or "")
    if checkpoint:
        ck_bucket, ck_prefix = _split_s3(checkpoint)
        if owned:
            ck_prefix = _scoped(b, ck_bucket, ck_prefix, "properties.stream.checkpoint")
            home = f"{_CHECKPOINTS}/{b.ctx.product}/"
            if ck_bucket != bucket or not ck_prefix.startswith(home):
                raise UnsupportedBindingError(
                    "spark-checkpoint-scope",
                    f"{b.where}: properties.stream.checkpoint is outside the product's "
                    f"checkpoints (s3://{b.ctx.shown(bucket)}/{home}), and the role forge "
                    "creates would "
                    "get write access wherever it points.",
                    (
                        "Leave properties.stream.checkpoint out: it defaults to "
                        f"s3://{b.ctx.shown(bucket)}/{home}{b.build_id}/.",
                        f"Or put it under s3://{b.ctx.shown(bucket)}/{home}.",
                        "Or set parameters.spark.executionRoleArn to a role the operator "
                        "scoped.",
                    ),
                )
        by_bucket.setdefault(ck_bucket, ([], []))[0].append(ck_prefix)
    if b.runtime == "glue":
        by_bucket[bucket][0].append(b.artifacts + "tmp/")
    x = b.ctx.x
    return tuple(
        aws_identity.S3Grant(
            bucket=x(name),
            write=tuple(x(p) for p in dict.fromkeys(write)),
            read=tuple(x(p) for p in dict.fromkeys(read)),
        )
        for name, (write, read) in by_bucket.items()
    )


def _dead_letters(
    b: _Build, contract: Mapping[str, Any], expose: Mapping[str, Any], data_prefix: str, iam: bool
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """The stream's dead letters on AWS: ``(table, its prefix, topic)``. The topic only on
    MSK with IAM, where the role needs a grant to write it."""
    dead = b.config.get("deadLetter") or {}
    if dead.get("sink") == "topic":
        return None, None, (dead.get("topic") if iam else None)
    if dead.get("sink") != "table":
        return None, None, None
    table = dead["table"].split(".", 1)[1]
    _refuse_unsupported_dead_letter_table(b, contract, expose, data_prefix)
    return table, _dead_letter_prefix(data_prefix, table), None


def _dead_letter_grant(b: _Build, bucket: str, prefix: str, owned: bool, shared: bool) -> str:
    """The dead letters' prefix a role forge creates writes, checked as the output's is."""
    if not owned:
        return prefix
    prefix = _scoped(b, bucket, prefix, "the dead letters' location")
    if shared:
        _refuse_unless_products_prefix(b.where, b.ctx, bucket, prefix)
    return prefix


def _dead_letter_prefix(data_prefix: str, dead_table: str) -> str:
    """The dead letters' table location: beside the output's, never inside it (a table's
    orphan-file cleanup would delete another table's files under its location)."""
    parent = data_prefix.rstrip("/").rpartition("/")[0]
    return f"{parent}/{dead_table}/" if parent else f"{dead_table}/"


def _refuse_unsupported_dead_letter_table(
    b: _Build, contract: Mapping[str, Any], expose: Mapping[str, Any], data_prefix: str
) -> None:
    """Refuse a dead-letter table forge cannot place or govern as it does the output: beside
    a bucket-root output (it would sit inside its location), beside a location forge
    registers with Lake Formation (it would need a registration of its own), or beside an
    output whose lifecycle expires its objects (the rule would not reach the dead letters,
    which hold the records' raw bytes)."""
    from . import aws as _aws  # imported here: aws imports this module

    binding = expose.get("binding") or {}
    gov = (binding.get("governance") or {}).get("lakeFormation") or {}
    lifecycle = expose.get("lifecycle")
    problem = None
    if not data_prefix.strip("/"):
        problem = (
            "the output's location is its bucket's root, and a table of the dead letters' own "
            "would sit inside it"
        )
    elif isinstance(gov, Mapping) and gov.get("registerLocation"):
        placement = _aws._placement(resolve_packaging(contract), expose)
        if _aws._lf_location(binding.get("location") or {}, placement)[0]:
            problem = (
                "forge registers the output's location with Lake Formation "
                "(registerLocation), and the dead letters' location would need a "
                "registration of its own, which forge does not emit yet"
            )
    elif isinstance(lifecycle, Mapping) and lifecycle.get("expire") is True:
        problem = (
            "the output's lifecycle expires its objects, and the rule would not reach the dead "
            "letters' table, which holds the records' raw bytes"
        )
    if problem:
        raise UnsupportedBindingError(
            "spark-dead-letters-table-unsupported",
            f"{b.where}: properties.stream.deadLetter writes a table of its own beside the "
            f"output, and {problem}.",
            (
                "Send the dead letters to a topic (deadLetter.sink: topic), whose retention is "
                "the topic's.",
            ),
        )


def _emit_dead_letter_table(
    blocks: SparkBlocks,
    contract: Mapping[str, Any],
    expose: Mapping[str, Any],
    b: _Build,
    dead_table: str,
    dead_prefix: str,
    principal: Any,
) -> None:
    """The dead letters' Iceberg table, emitted as :mod:`aws` emits the output's (Glue writes
    its first metadata), beside it; and the job's grant to commit to it, as for the output:
    always under the output's ``governance.lakeFormation``, and without one where the
    account's Lake Formation settings govern new tables (``_lake_formation_default``)."""
    from fluid_build.iac.governance_labels import governance_labels
    from fluid_build.spark.dead_letter import SCHEMA

    from . import aws as _aws  # imported here: aws imports this module

    binding = expose.get("binding") or {}
    loc = dict(binding.get("location") or {})
    database = str(loc.get("database"))
    placement = _aws._placement(resolve_packaging(contract), expose)
    labels = governance_labels(contract, expose)
    tags = _aws._tags_for({**labels, "managed_by": "fluid", "fluid_contract": b.ctx.cid}, placement)
    emitted: Dict[str, Dict[str, Any]] = {}
    _aws._emit_glue(
        emitted,
        [dict(column) for column in SCHEMA],
        b.ctx.cid,
        tags,
        # The output's binding, at the dead letters' own table and prefix; unpartitioned
        # (the default layout), whatever the output's partitionBy says.
        binding={
            **binding,
            "format": "iceberg",
            "location": {**loc, "table": dead_table, "path": dead_prefix},
        },
        contract=contract,
        placement=placement,
        labels=labels,
        expose=f"{expose.get('exposeId')} (dead letters)",
    )
    key = safe_ident(f"{b.ctx.cid}_{database}_{dead_table}")
    body = emitted["aws_glue_catalog_table"][key]
    body["description"] = (
        f"The records the forge Spark build {b.build_id} could not land in "
        f"{database}.{loc.get('table')}, with why and where they were read"
    )
    tables = blocks.resources.setdefault("aws_glue_catalog_table", {})
    if key in tables:
        raise UnsupportedBindingError(
            "spark-address-conflict",
            f"{b.where}: properties.stream.deadLetter.table {database}.{dead_table} is another "
            "Spark build's dead-letter table.",
            ("Give each build's dead letters a table of their own.",),
        )
    tables[key] = body
    if principal is None:
        return
    grant: Dict[str, Any] = {
        "principal": principal,
        "permissions": ["ALTER", "DESCRIBE", "INSERT", "SELECT"],
        "table": [
            {
                "database_name": tofu_ref(f"aws_glue_catalog_table.{key}.database_name"),
                "name": tofu_ref(f"aws_glue_catalog_table.{key}.name"),
            }
        ],
    }
    if not (binding.get("governance") or {}).get("lakeFormation"):
        # As for the output (``aws._emit_default_spark_writer_grants``): granted where the
        # account's Lake Formation settings govern new tables, decided at plan.
        if not _lake_formation_default(binding):
            return
        settings = safe_ident(f"{b.ctx.cid}_lf_settings")
        grant = {"count": _aws._where_lake_formation_governs(settings), **grant}
    blocks.resources.setdefault("aws_lakeformation_permissions", {})[
        safe_ident(f"{b.ctx.cid}_lf_spark_dead_letters_{b.build_id}")
    ] = grant


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
    b: _Build,
    msk: Optional[aws_identity.MskSource],
    applications: Dict[str, Any],
) -> Any:
    """The EMR Serverless application id the job runs in, emitting it when isolated."""
    cfg, ctx, where = b.cfg, b.ctx, b.where
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
    arn = msk.arn if msk else None
    if app_key in applications:
        first_release, first_cluster = applications[app_key]
        if release != first_release or arn != first_cluster:
            raise UnsupportedBindingError(
                "spark-emr-application-conflict",
                f"{where} asks for the product's isolated EMR Serverless application with a "
                "different releaseLabel or MSK cluster than another build of the product; "
                "one application has one release and one network.",
                ("Give every isolated EMR Serverless build the same releaseLabel and cluster.",),
            )
        return tofu_ref(f"aws_emrserverless_application.{app_key}.id")
    applications[app_key] = (release, arn)
    body: Dict[str, Any] = {
        "name": names.application_name(ctx.product),
        "release_label": release,
        "type": "spark",
        "auto_stop_configuration": {
            "enabled": True,
            "idle_timeout_minutes": APPLICATION_IDLE_MINUTES,
        },
        "maximum_capacity": {"cpu": APPLICATION_MAX_CPU, "memory": APPLICATION_MAX_MEMORY},
        # AWS fills the job queue's defaults (15 concurrent runs, 360-minute timeout) at
        # create, and a plan to blank them would re-read every document that names the
        # application (measured in ca-central-1). forge does not size the queue.
        "lifecycle": {"ignore_changes": ["scheduler_configuration"]},
        "tags": {
            "managed_by": "fluid",
            "fluid_contract": ctx.cid,
            names.PRODUCT_TAG: ctx.product,
        },
    }
    if msk is not None:
        _same_region(b, msk, "the product's isolated EMR Serverless application")
        cluster = _msk_cluster_data(blocks, app_key, msk)
        body["network_configuration"] = {
            "subnet_ids": tofu_ref(f"{cluster}.broker_node_group_info[0].client_subnets"),
            "security_group_ids": tofu_ref(f"{cluster}.broker_node_group_info[0].security_groups"),
        }
    blocks.resources.setdefault("aws_emrserverless_application", {})[app_key] = body
    return tofu_ref(f"aws_emrserverless_application.{app_key}.id")


def _msk_cluster_data(blocks: SparkBlocks, key: str, msk: aws_identity.MskSource) -> str:
    blocks.data.setdefault("aws_msk_cluster", {})[key] = {
        "cluster_name": msk.name,
        "region": msk.region,
    }
    return f"data.aws_msk_cluster.{key}"


def _same_region(b: _Build, msk: aws_identity.MskSource, what: str) -> None:
    """A job's network is the cluster's subnets, which exist in the cluster's region only."""
    if isinstance(b.region, str) and b.region != msk.region:
        raise UnsupportedBindingError(
            "spark-msk-region",
            f"{b.where}: {what} would sit in the MSK cluster's subnets, but the cluster is in "
            f"{msk.region} and the output in {b.region}.",
            ("Read a cluster in the output's region, or run the job by bootstrapServers.",),
        )


def _trust(blocks: SparkBlocks, b: _Build, role_name: str, application_id: Any) -> Dict[str, Any]:
    issuer = namespace = None
    if b.runtime == "emr-eks":
        key = b.key
        blocks.data.setdefault("aws_emrcontainers_virtual_cluster", {})[key] = {
            "virtual_cluster_id": b.cfg["virtualClusterId"]
        }
        vc = f"data.aws_emrcontainers_virtual_cluster.{key}.container_provider[0]"
        blocks.data.setdefault("aws_eks_cluster", {})[key] = {"name": tofu_ref(f"{vc}.id")}
        issuer = tofu_ref(
            f'trimprefix(data.aws_eks_cluster.{key}.identity[0].oidc[0].issuer, "https://")'
        )
        namespace = tofu_ref(f"{vc}.info[0].eks_info[0].namespace")
    return aws_identity.trust_document(
        b.runtime,
        account=b.ctx.account,
        # The application is in the provider's region, whatever the output's is.
        region=_provider_region(blocks),
        role_name=role_name,
        application_id=application_id,
        eks_issuer=issuer,
        eks_namespace=namespace,
        glue_job=(names.glue_job_name(b.ctx.product, b.build_id) if b.runtime == "glue" else None),
    )


def _glue_job(blocks: SparkBlocks, b: _Build, mode: str, role_arn: Any) -> None:
    streaming = mode == "streaming"
    blocks.resources.setdefault("aws_glue_job", {})[b.key] = {
        "name": names.glue_job_name(b.ctx.product, b.build_id),
        "description": f"The forge Spark build {b.build_id}",
        "role_arn": role_arn,
        "glue_version": b.cfg.get("glueVersion") or DEFAULT_GLUE_VERSION,
        "command": {
            "name": "gluestreaming" if streaming else "glueetl",
            "script_location": b.object_uri(b.runner_key),
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
        "tags": dict(b.tags),
    }


def _glue_connection(blocks: SparkBlocks, b: _Build, msk: aws_identity.MskSource) -> None:
    """A Glue ``NETWORK`` connection in one of the MSK cluster's subnets, with its
    security groups: what puts a Glue job's workers where the brokers are."""
    _same_region(b, msk, "the Glue job's network connection")
    key = b.key
    cluster = _msk_cluster_data(blocks, key, msk)
    blocks.data.setdefault("aws_subnet", {})[key] = {
        "id": tofu_ref(f"tolist({cluster}.broker_node_group_info[0].client_subnets)[0]"),
        "region": msk.region,
    }
    from . import aws as _aws  # imported here: aws imports this module

    name = bounded_ident(f"fluid-{b.ctx.product}-{b.build_id}-msk", 255, "-")
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
        "tags": dict(b.tags),
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
    region_expr = _provider_region(blocks)
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


def _emit_caller_policy(
    blocks: SparkBlocks,
    ctx: _Ctx,
    runs: List[aws_identity.CallerRun],
    tags: Mapping[str, str],
) -> None:
    """A managed policy that lets a scheduler start, watch and stop the product's runs.

    A build that runs to completion is started by its scheduler through ``fluid apply
    --mode amend-and-build`` (an MWAA worker, as the environment's execution role), which
    the platform owns: the policy is the product's, for the platform to attach to that role.
    It covers the run only: the worker runs the whole apply, ``tofu`` included, so that role
    also needs what the apply provisions with and the state backend, as for any scheduled
    build.
    """
    key = safe_ident(f"{ctx.cid}_spark_caller")
    blocks.data.setdefault("aws_iam_policy_document", {})[key] = aws_identity.caller_document(
        runs, account=ctx.account, region=_provider_region(blocks)
    )
    blocks.resources.setdefault("aws_iam_policy", {})[key] = {
        "name": names.caller_policy_name(ctx.product),
        "description": (
            f"Starts, watches and stops the forge Spark runs of {ctx.product}; attach it to "
            "the scheduler's role (the MWAA execution role)"
        ),
        "policy": tofu_ref(f"data.aws_iam_policy_document.{key}.json"),
        "tags": {**tags, names.PRODUCT_TAG: ctx.product},
    }


#: What a forge Spark build runs with, by resource type and the end of its name in the module
#: (the keys the emitters above write: ``<contract>_<build>_spark`` and what hangs off it, the
#: product's ``<contract>_spark`` application, ``_spark_caller`` policy and ``_spark_logs``
#: key, the ``_lf_writer_<8 hex>`` registration role, and the job role's own Lake Formation
#: grants: ``_lf_spark_writer_<table>_<n>`` on the table it writes and
#: ``_lf_spark_dead_letters_<build>`` on its stream's dead-letter table, counted at plan where
#: the account's settings decide them, so a plan names them ``...[0]``). None holds data: a
#: destroy deletes runners, job configs, roles, policies, jobs, an application, grants, and the
#: build's log group with the key its logs are encrypted with. A bucket's own key ends in
#: ``_kms`` and stays gated, and a grant the binding's ``governance.lakeFormation`` declares
#: (``_lf_grant_*``) is not the build's.
_SPARK_DEFINITIONS: Dict[str, "re.Pattern[str]"] = {
    "aws_s3_object": re.compile(r"_spark_(runner|config)$"),
    "aws_s3_object_copy": re.compile(r"_spark_jar_[0-9]+$"),
    "aws_cloudwatch_log_group": re.compile(r"_spark$"),
    "aws_kms_key": re.compile(r"_spark_logs$"),
    "aws_iam_role": re.compile(r"(_spark|_lf_writer_[0-9a-f]{8})$"),
    "aws_iam_role_policy": re.compile(r"(_spark|_lf_writer_[0-9a-f]{8})$"),
    "aws_iam_policy": re.compile(r"_spark(_caller)?$"),
    "aws_glue_job": re.compile(r"_spark$"),
    "aws_glue_connection": re.compile(r"_spark$"),
    "aws_emrserverless_application": re.compile(r"_spark$"),
    "aws_lakeformation_permissions": re.compile(
        r"_lf_spark_(writer_\w+_[0-9]+|dead_letters_\w+)(\[0\])?$"
    ),
}


def spark_definition(address: str, kind: str) -> bool:
    """``address`` (of type ``kind``, in the module's root) is something a Spark build runs
    with, which holds no data (:data:`_SPARK_DEFINITIONS`)."""
    pattern = _SPARK_DEFINITIONS.get(kind)
    prefix = f"{kind}."
    if pattern is None or not address.startswith(prefix):
        return False
    return bool(pattern.search(address[len(prefix) :]))


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
