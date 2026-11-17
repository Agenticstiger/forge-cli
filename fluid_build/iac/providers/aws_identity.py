# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The IAM identity of a forge Spark job, and what the operator lets a contract choose.

Each ``engine: spark`` build (``fluid_build/spark``) runs as one role. Without
``parameters.spark.executionRoleArn`` forge creates that role per build. Its inline
policy grants only what the job config (``job.json``) reads and writes:

- S3: read and write on the output table's data prefix and on the build's checkpoint
  (``<bucket>/_checkpoints/<product>/<build>/``); read only on the build's artifacts
  (``<bucket>/_fluid/spark/<product>/<build>/``, the runner and ``job.json``); list on
  those prefixes alone. Glue also gets its ``--TempDir`` under the artifact prefix.
- Glue: ``GetDatabase``, ``GetTable`` and ``UpdateTable`` on the output's catalog,
  database and table ARNs. These are the calls Iceberg's ``GlueCatalog`` makes to load a
  table and commit to it.
- Lake Formation: ``GetDataAccess``. It has no resource-level form, so Lake Formation's
  own grants decide what the role reads.
- KMS: ``Decrypt``/``GenerateDataKey`` through S3 only (``kms:ViaService``), for objects
  of the bucket's prefixes (``kms:EncryptionContext:aws:s3:arn``), whatever key the
  bucket uses. Also ``Decrypt`` through Secrets Manager for the job's secrets only.
- MSK IAM: ``Connect`` on the cluster, ``DescribeTopic``/``ReadData`` on the topic, and
  ``DescribeGroup``/``AlterGroup`` on the consumer groups the Kafka source creates.
  ReadData needs Connect, DescribeTopic and AlterGroup. AlterGroup needs DescribeGroup.
  (MSK developer guide, *IAM access control*: "Semantics of actions and resources".)
- Secrets Manager: ``GetSecretValue`` on the ``maskAtIngest`` key and on an
  ``aws-sm://`` registry credential.
- CloudWatch Logs: write to the build's own log group.

The role's trust follows the runtime:

- ``emr-serverless``: ``emr-serverless.amazonaws.com``, for this account and, by
  ``aws:SourceArn``, only the application that runs the job (the confused-deputy guard in
  the EMR Serverless user guide, *Job runtime roles*).
- ``glue``: ``glue.amazonaws.com``, for this account.
- ``emr-eks``: the EKS cluster's OIDC provider, for the service accounts EMR on EKS
  creates in the virtual cluster's namespace. The statement is the one
  ``aws emr-containers update-role-trust-policy`` writes, with the role name in the same
  base36 encoding (awscli ``customizations/emrcontainers``, Apache-2.0). The virtual
  cluster and its EKS cluster are read at plan time.
- ``emr-ec2``: a step runs as the cluster's EC2 instance profile, which belongs to the
  cluster, not to the product. forge creates no role here. It creates the same policy as
  a managed policy for the cluster owner to attach to that instance profile's role.

``executionRoleArn`` set means forge only references the role, and the operator decides
which roles a contract may name (:data:`EXECUTION_ROLE_ALLOWLIST_ENV`). ``sparkConf``
keys pass a fixed allowlist of tuning settings. A key that can load code, change the
job's identity or credentials, or move its catalog, warehouse or checkpoint is always
refused. Any other key needs the operator's :data:`SPARK_CONF_ALLOWLIST_ENV`. Both
follow the operator-policy severities of the Iceberg catalog-auth decision (#818): a
missing operator entry warns in ``fluid validate`` and is refused when the IaC is
generated or applied.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build._errors import doc_url
from fluid_build.spark.compiler import TARGET_ALLOWLIST_ENV as _TARGET_ALLOWLIST_ENV

#: Operator-only: the roles a contract's ``executionRoleArn`` may name. An entry is a
#: role ARN, or an ARN prefix that ends in ``/`` (a role path). Unset allows none.
EXECUTION_ROLE_ALLOWLIST_ENV = "FLUID_SPARK_EXECUTION_ROLE_ALLOWLIST"
#: Operator-only: ``sparkConf`` keys beyond the built-in tuning list. An entry is a key,
#: or a prefix that ends in ``*``. It never admits a key in :data:`DENIED_SPARK_CONF`.
SPARK_CONF_ALLOWLIST_ENV = "FLUID_SPARK_CONF_ALLOWLIST"
#: Operator-only: the existing runtimes a contract may name, as ``<runtime>:<id>``
#: (``emr-serverless:00f...``, ``emr-ec2:j-...``, ``emr-eks:<virtual cluster id>``), and
#: the MSK clusters it may read, as ``msk:<cluster ARN>``. The build's role trusts the
#: runtime and reads the cluster, and the scheduler's policy may start, cancel and add
#: steps there, so a contract naming another team's would reach it. Unset allows none; a
#: product's own isolated EMR Serverless application needs no entry. One list with the
#: Spark compiler, which applies the MSK entries too.
TARGET_ALLOWLIST_ENV = _TARGET_ALLOWLIST_ENV
#: The field that names the existing runtime, per runtime.
_TARGET_FIELD = {
    "emr-serverless": "applicationId",
    "emr-ec2": "clusterId",
    "emr-eks": "virtualClusterId",
}

_OPERATOR_ONLY = (
    "in the OPERATOR environment (a shell or CI variable; a project .env is ignored "
    "for every *_ALLOWLIST setting)"
)

#: Always refused, whatever the operator lists. A key here, or under a prefix that ends
#: in ``.``, could load code into the job (jars, Python files, plugins, extensions,
#: class paths, JVM options), change who the job is or which credentials it uses
#: (Hadoop and AWS credential providers, environment variables, Kubernetes pod
#: settings), or move what the job writes (its catalog, warehouse and checkpoint, which
#: the job config owns), or switch off a setting the runner relies on.
DENIED_SPARK_CONF: Tuple[str, ...] = (
    "spark.jars",
    "spark.jars.*",
    "spark.files",
    "spark.files.*",
    "spark.archives",
    "spark.submit.*",
    "spark.pyspark.*",
    "spark.python.*",
    "spark.plugins",
    "spark.plugins.*",
    "spark.extraListeners",
    "spark.sql.extensions",
    "spark.sql.catalog.*",
    "spark.sql.catalogImplementation",
    "spark.sql.defaultCatalog",
    "spark.sql.warehouse.dir",
    "spark.sql.streaming.checkpointLocation",
    "spark.sql.queryExecutionListeners",
    "spark.sql.streaming.streamingQueryListeners",
    "spark.sql.variable.substitute",
    "spark.sql.session.timeZone",
    "spark.sql.mapKeyDedupPolicy",
    # A class a setting names is code the job loads.
    "spark.sql.adaptive.customCostEvaluatorClass",
    "spark.sql.streaming.stateStore.providerClass",
    "spark.driver.extraClassPath",
    "spark.driver.extraJavaOptions",
    "spark.driver.extraLibraryPath",
    "spark.executor.extraClassPath",
    "spark.executor.extraJavaOptions",
    "spark.executor.extraLibraryPath",
    "spark.driver.userClassPathFirst",
    "spark.executor.userClassPathFirst",
    "spark.driver.defaultJavaOptions",
    "spark.executor.defaultJavaOptions",
    "spark.executorEnv.*",
    "spark.yarn.*",
    "spark.kubernetes.*",
    "spark.emr-serverless.driverEnv.*",
    "spark.emr-serverless.executorEnv.*",
    "spark.emr-serverless.lakeformation.*",
    "spark.hadoop.*",
    "spark.kerberos.*",
    "spark.security.*",
    "spark.authenticate",
    "spark.authenticate.*",
    "spark.ssl.*",
    "spark.redaction.*",
    "spark.eventLog.*",
    "spark.ui.*",
    "spark.master",
    "spark.app.*",
)

#: Allowed with no operator entry: sizing and tuning settings, which change how fast and
#: how wide the job runs, never what it runs or as whom. Exact keys, or prefixes that
#: end in ``*``.
DEFAULT_SPARK_CONF: Tuple[str, ...] = (
    "spark.driver.cores",
    "spark.driver.memory",
    "spark.driver.memoryOverhead",
    "spark.driver.maxResultSize",
    "spark.executor.cores",
    "spark.executor.memory",
    "spark.executor.memoryOverhead",
    "spark.executor.instances",
    "spark.dynamicAllocation.*",
    "spark.default.parallelism",
    "spark.memory.*",
    "spark.network.timeout",
    "spark.task.maxFailures",
    "spark.sql.shuffle.partitions",
    "spark.sql.adaptive.*",
    "spark.sql.files.maxPartitionBytes",
    "spark.sql.files.maxRecordsPerFile",
    "spark.sql.autoBroadcastJoinThreshold",
    "spark.sql.streaming.minBatchesToRetain",
    "spark.sql.iceberg.*",
    "spark.emr-serverless.driver.disk",
    "spark.emr-serverless.executor.disk",
    "spark.emr-serverless.driver.disk.type",
    "spark.emr-serverless.executor.disk.type",
)

_ROLE_ARN = re.compile(r"arn:aws[a-zA-Z0-9-]*:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]{1,512}")
#: IAM's limit on a role name.
ROLE_NAME_LIMIT = 64


def _matches(key: str, entries: Sequence[str]) -> bool:
    """``key`` is an entry, or starts with an entry that ends in ``*`` (the one grammar the
    runs lane's ``_desired.py`` reads too)."""
    return any(key == e or (e.endswith("*") and key.startswith(e[:-1])) for e in entries)


def _entries(name: str, environ: Optional[Mapping[str, str]]) -> List[str]:
    env = os.environ if environ is None else environ
    return [e.strip() for e in (env.get(name) or "").split(",") if e.strip()]


def denied_spark_conf(conf: Mapping[str, Any]) -> List[str]:
    """The ``sparkConf`` keys no operator entry admits."""
    return sorted(k for k in conf if _matches(str(k), DENIED_SPARK_CONF))


def unlisted_spark_conf(
    conf: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> List[str]:
    """The keys that are neither denied nor allowed by default or by the operator."""
    listed = [*DEFAULT_SPARK_CONF, *_entries(SPARK_CONF_ALLOWLIST_ENV, environ)]
    return sorted(
        str(k)
        for k in conf
        if not _matches(str(k), DENIED_SPARK_CONF) and not _matches(str(k), listed)
    )


def role_listed(arn: str, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the operator lets a contract name ``arn`` as its execution role."""
    entries = _entries(EXECUTION_ROLE_ALLOWLIST_ENV, environ)
    return any(arn == e or (e.endswith("/") and arn.startswith(e)) for e in entries)


def _spark_conf(build: Mapping[str, Any]) -> Mapping[str, Any]:
    from fluid_build.spark.compiler import spark_parameters

    conf = (spark_parameters(build) or {}).get("sparkConf") or {}
    if not isinstance(conf, Mapping):
        raise ValueError(f"Spark build {build.get('id')}: parameters.spark.sparkConf: an object")
    return conf


def identity_findings(
    build: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> Tuple[List[str], List[str]]:
    """``(errors, missing operator entries)`` for one Spark build's role and ``sparkConf``.

    An error fails at every stage. A missing entry is a warning in ``fluid validate``
    and a refusal in :func:`refuse_unless_identity_allowed`.
    """
    from fluid_build.spark.compiler import spark_parameters

    bid = build.get("id")
    errors: List[str] = []
    missing: List[str] = []
    try:
        conf = _spark_conf(build)
    except ValueError as exc:
        return [str(exc)], []
    denied = denied_spark_conf(conf)
    if denied:
        errors.append(
            f"Spark build {bid}: parameters.spark.sparkConf sets {', '.join(denied)}. These "
            "keys can load code into the job, change its identity or credentials, or move "
            "its catalog, warehouse or checkpoint, which the job config owns, so no "
            "contract may set them"
        )
    unlisted = unlisted_spark_conf(conf, environ)
    if unlisted:
        missing.append(
            f"Spark build {bid}: parameters.spark.sparkConf sets {', '.join(unlisted)}, "
            f"which {SPARK_CONF_ALLOWLIST_ENV} does not list"
        )
    cfg = spark_parameters(build) or {}
    field = _TARGET_FIELD.get(str(cfg.get("runtime")))
    target = cfg.get(field) if field else None
    if isinstance(target, str) and target:
        entry = f"{cfg['runtime']}:{target}"
        if entry not in _entries(TARGET_ALLOWLIST_ENV, environ):
            missing.append(
                f"Spark build {bid}: parameters.spark.{field} {target} is not in "
                f"{TARGET_ALLOWLIST_ENV} (as {entry})"
            )
    source = cfg.get("source")
    cluster = source.get("mskClusterArn") if isinstance(source, Mapping) else None
    if isinstance(cluster, str) and cluster:
        from fluid_build.spark.compiler import cluster_listed

        if not cluster_listed(cluster, environ):
            missing.append(
                f"Spark build {bid}: parameters.spark.source.mskClusterArn {cluster} is not "
                f"in {TARGET_ALLOWLIST_ENV} (as msk:{cluster})"
            )
    arn = cfg.get("executionRoleArn")
    if arn is not None:
        if not (isinstance(arn, str) and _ROLE_ARN.fullmatch(arn)):
            errors.append(f"Spark build {bid}: parameters.spark.executionRoleArn: a role ARN")
        elif not role_listed(arn, environ):
            missing.append(
                f"Spark build {bid}: parameters.spark.executionRoleArn {arn} is not in "
                f"{EXECUTION_ROLE_ALLOWLIST_ENV}"
            )
    return errors, missing


#: The runtime fields the IaC names, each with the 0.7.7 schema's own pattern: the emit
#: checks them itself, as ``fluid generate iac`` does not run the schema first.
_RUNTIME_FIELDS = {
    "applicationId": re.compile(r"[0-9a-z]{1,64}"),
    "clusterId": re.compile(r"j-[0-9A-Za-z]{1,254}"),
    "virtualClusterId": re.compile(r"[0-9a-z]{1,64}"),
    "glueVersion": re.compile(r"[0-9]+\.[0-9]+"),
    "releaseLabel": re.compile(r"emr-[0-9]+\.[0-9]+\.[0-9]+"),
}
#: An MSK cluster ARN with the name and id the topic and group ARNs are built from.
MSK_ARN = re.compile(
    r"arn:aws[a-z-]*:kafka:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):cluster/"
    r"(?P<name>[A-Za-z0-9_.-]{1,64})/(?P<uuid>[A-Za-z0-9-]{1,128})"
)


def runtime_errors(build: Mapping[str, Any]) -> List[str]:
    """What keeps the IaC from naming the build's runtime: a field of the wrong shape, a
    runtime without the cluster it needs, an MSK ARN without its cluster id, or a batch
    build without an operator-scoped role.

    A batch build runs its own SQL over catalog tables the contract does not list, so a
    role forge derives could read none of them: it needs ``executionRoleArn``.
    """
    from fluid_build.spark.compiler import spark_parameters

    bid = build.get("id")
    try:
        cfg = spark_parameters(build) or {}
    except ValueError as exc:
        return [str(exc)]
    errors = []
    for name, pattern in _RUNTIME_FIELDS.items():
        value = cfg.get(name)
        if value is not None and not (isinstance(value, str) and pattern.fullmatch(value)):
            errors.append(f"Spark build {bid}: parameters.spark.{name}: expected {pattern.pattern}")
    if cfg.get("runtime") == "emr-eks" and not cfg.get("virtualClusterId"):
        errors.append(f"Spark build {bid}: runtime emr-eks needs parameters.spark.virtualClusterId")
    source = cfg.get("source")
    arn = source.get("mskClusterArn") if isinstance(source, Mapping) else None
    if isinstance(arn, str) and not MSK_ARN.fullmatch(arn):
        errors.append(
            f"Spark build {bid}: parameters.spark.source.mskClusterArn: expected the full "
            "cluster ARN, arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid>"
        )
    props = build.get("properties")
    stream = props.get("stream") if isinstance(props, Mapping) else None
    batch = cfg.get("mode") == "batch" or (cfg.get("mode") is None and stream is None)
    # On emr-ec2 a step runs as the cluster's own role, so there is no role to require.
    if batch and not cfg.get("executionRoleArn") and cfg.get("runtime") != "emr-ec2":
        errors.append(
            f"Spark build {bid}: a batch job reads tables its SQL names, which forge cannot "
            "grant a role it derives; set parameters.spark.executionRoleArn to a role the "
            "operator scoped to those tables"
        )
    return errors


def refuse_unless_identity_allowed(
    product_id: Any, build: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise :class:`~fluid_build.spark.policy.SparkPolicyError` unless the build's role
    and ``sparkConf`` pass the operator's policy. Called before anything is emitted."""
    from fluid_build.spark.policy import SparkPolicyError

    errors, missing = identity_findings(build, environ)
    if not errors and not missing:
        return
    bid = build.get("id")
    raise SparkPolicyError(
        what=(
            f"Spark build {bid!r} names a role, a runtime or Spark settings the operator "
            "has not allowed"
        ),
        why=" ".join(f"{m}." for m in [*errors, *missing])
        + " A contract that names an existing role runs the job, and the code the "
        "product's SQL reaches, with everything that role can do; a Spark setting can "
        "change what the job loads and as whom it runs. Unset lists allow nothing.",
        fix=(
            "Leave executionRoleArn out to have forge create a least-privilege role for "
            "the build. Otherwise, once the role and settings are reviewed, have the "
            f"operator list them {_OPERATOR_ONLY}: {EXECUTION_ROLE_ALLOWLIST_ENV} takes a "
            "role ARN or a path prefix ending in '/'; "
            f"{TARGET_ALLOWLIST_ENV} takes <runtime>:<id> of an existing application, "
            "cluster or virtual cluster, or msk:<cluster ARN>; "
            f"{SPARK_CONF_ALLOWLIST_ENV} takes a key or a prefix ending in '*'. A key "
            "that loads code, sets credentials or moves the catalog or checkpoint cannot "
            "be listed."
        ),
        doc=doc_url(),
    )


def _spark_builds_or_empty(contract: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    from fluid_build.spark.compiler import spark_builds
    from fluid_build.streaming.compiler import managed_lane_enabled

    if not managed_lane_enabled(contract):
        return []
    try:
        return spark_builds(contract)
    except ValueError:
        return []  # the Spark compiler's validation reports it


def validation_errors(contract: Mapping[str, Any]) -> List[str]:
    """What ``fluid validate`` refuses about the contract's Spark roles, settings and
    runtime fields."""
    return [
        e
        for b in _spark_builds_or_empty(contract)
        for e in [*identity_findings(b)[0], *runtime_errors(b)]
    ]


def validation_warnings(contract: Mapping[str, Any]) -> List[str]:
    """The operator entries the contract's Spark builds still need (#818: warn here)."""
    warnings = [
        f"{m}; the IaC is refused at generate and apply until the operator lists it"
        for b in _spark_builds_or_empty(contract)
        for m in identity_findings(b)[1]
    ]
    return warnings + _glue_jar_warnings(contract)


def _glue_jar_warnings(contract: Mapping[str, Any]) -> List[str]:
    """A Glue build that needs pinned jars while the operator names no jar prefix."""
    from fluid_build.spark.compiler import compile_job_config, spark_parameters

    from . import aws_glue_jars

    out = []
    for build in _spark_builds_or_empty(contract):
        if (spark_parameters(build) or {}).get("runtime") != "glue":
            continue
        try:
            config = compile_job_config(
                contract,
                build,
                account_ref="000000000000",
                contract_sql_gate=False,
                host_gate=False,
            ).config
            unset = aws_glue_jars.needed_jars(config) and aws_glue_jars.jars_location() is None
        except Exception:  # noqa: BLE001 - the Spark compiler's validation reports it
            continue
        if unset:
            out.append(
                f"Spark build {build.get('id')}: Confluent Avro on Glue needs the pinned "
                f"jars, and {aws_glue_jars.JARS_URL_ENV} names no prefix; the IaC is refused "
                "at generate and apply until the operator sets it"
            )
    return out


# --- the role and its policy -------------------------------------------------


def base36(text: str) -> str:
    """``text`` as EMR on EKS encodes a role name in its service-account names.

    The bytes of the name as one big-endian number, in lower-case base 36: awscli
    ``customizations/emrcontainers/base36.py`` (Apache-2.0), which
    ``update-role-trust-policy`` uses.
    """
    number = 0
    for char in text:
        number = number * 256 + ord(char)
    alphabet, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while number:
        number, digit = divmod(number, 36)
        out = alphabet[digit] + out
    return out or "0"


@dataclass(frozen=True)
class S3Grant:
    """One bucket's prefixes the job reads (and maybe writes)."""

    bucket: Any  # a name, or an emitter expression holding the account token
    write: Tuple[Any, ...] = ()
    read: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class MskSource:
    """The MSK cluster a streaming job reads, from its ARN."""

    arn: str
    region: str
    account: str
    name: str
    uuid: str
    topic: str
    group_prefix: str


@dataclass(frozen=True)
class JobAccess:
    """Everything the job's policy names. Strings are contract text the renderer escapes,
    or emitter expressions (``TofuExpr``) built by :mod:`aws_ingest`."""

    region: Any
    account: Any
    s3: Tuple[S3Grant, ...]
    database: str
    table: str
    log_group_arn: Any
    msk: Optional[MskSource] = None
    secret_arns: Tuple[Any, ...] = ()
    #: The job's workers sit in a VPC through a Glue NETWORK connection.
    glue_vpc: bool = False
    #: That connection's subnet ARN: the only subnet whose interfaces the role may tag
    #: and delete.
    glue_subnet_arn: Any = None
    #: Exact objects the job reads besides its own prefixes (Glue's pinned jars).
    objects: Tuple[Any, ...] = ()


def secret_resources(arn: Any) -> List[Any]:
    """A secret as policy resources: as written, and with the random ``-??????`` suffix
    Secrets Manager appends, since a name or partial ARN cannot be told from a full ARN
    by its shape (``prod/mask-secret`` ends in six letters too). Keeps an account
    interpolation an expression."""
    return [arn, _join(arn, "-??????")]


def _join(*parts: Any) -> Any:
    """Concatenate, keeping a ``TofuExpr`` part an expression (the others escaped)."""
    from ..naming import TofuExpr

    if not any(isinstance(p, TofuExpr) for p in parts):
        return "".join(str(p) for p in parts)
    out = []
    for p in parts:
        out.append(str(p) if isinstance(p, TofuExpr) else _escape(str(p)))
    return TofuExpr("".join(out))


def _escape(text: str) -> str:
    return text.replace("${", "$${").replace("%{", "%%{")


def access_document(access: JobAccess) -> Dict[str, Any]:
    """The job's least-privilege ``aws_iam_policy_document`` body."""
    region, account = access.region, access.account
    statements: List[Dict[str, Any]] = []
    object_arns: List[Any] = []
    for grant in access.s3:
        bucket_arn = _join("arn:aws:s3:::", grant.bucket)
        if grant.write:
            statements.append(
                {
                    "sid": f"ReadWrite{len(statements)}",
                    "effect": "Allow",
                    "actions": [
                        "s3:AbortMultipartUpload",
                        "s3:DeleteObject",
                        "s3:GetObject",
                        "s3:PutObject",
                    ],
                    "resources": [_join(bucket_arn, "/", p, "*") for p in grant.write],
                }
            )
        if grant.read:
            statements.append(
                {
                    "sid": f"Read{len(statements)}",
                    "effect": "Allow",
                    "actions": ["s3:GetObject"],
                    "resources": [_join(bucket_arn, "/", p, "*") for p in grant.read],
                }
            )
        prefixes = [*grant.write, *grant.read]
        statements.append(
            {
                "sid": f"List{len(statements)}",
                "effect": "Allow",
                "actions": ["s3:ListBucket"],
                "resources": [bucket_arn],
                "condition": [
                    {
                        "test": "StringLike",
                        "variable": "s3:prefix",
                        "values": [_join(p, "*") for p in prefixes],
                    }
                ],
            }
        )
        statements.append(
            {
                "sid": f"Locate{len(statements)}",
                "effect": "Allow",
                "actions": ["s3:GetBucketLocation"],
                "resources": [bucket_arn],
            }
        )
        # With an S3 Bucket Key the encryption context is the bucket's ARN; without one
        # it is the object's.
        object_arns.extend([bucket_arn, *(_join(bucket_arn, "/", p, "*") for p in prefixes)])
    if access.objects:
        statements.append(
            {
                "sid": "ReadJars",
                "effect": "Allow",
                "actions": ["s3:GetObject"],
                "resources": list(access.objects),
            }
        )
        object_arns.extend(dict.fromkeys(str(a).split("/", 1)[0] for a in access.objects))
        object_arns.extend(access.objects)
    statements.append(
        {
            "sid": "S3Kms",
            "effect": "Allow",
            "actions": ["kms:Decrypt", "kms:GenerateDataKey"],
            "resources": ["*"],
            "condition": [
                {
                    "test": "StringEquals",
                    "variable": "kms:ViaService",
                    "values": [_join("s3.", region, ".amazonaws.com")],
                },
                {
                    "test": "StringLike",
                    "variable": "kms:EncryptionContext:aws:s3:arn",
                    "values": object_arns,
                },
            ],
        }
    )
    glue = _join("arn:aws:glue:", region, ":", account, ":")
    statements.append(
        {
            "sid": "GlueTable",
            "effect": "Allow",
            "actions": ["glue:GetDatabase", "glue:GetTable", "glue:UpdateTable"],
            "resources": [
                _join(glue, "catalog"),
                _join(glue, "database/", access.database),
                _join(glue, "table/", access.database, "/", access.table),
            ],
        }
    )
    statements.append(
        {
            "sid": "LakeFormationCredentials",
            "effect": "Allow",
            "actions": ["lakeformation:GetDataAccess"],
            "resources": ["*"],
        }
    )
    msk = access.msk
    if msk is not None:
        scope = f"{msk.region}:{msk.account}"
        tail = f"{msk.name}/{msk.uuid}"
        statements.extend(
            [
                {
                    "sid": "MskConnect",
                    "effect": "Allow",
                    "actions": ["kafka-cluster:Connect"],
                    "resources": [msk.arn],
                },
                {
                    "sid": "MskTopic",
                    "effect": "Allow",
                    "actions": ["kafka-cluster:DescribeTopic", "kafka-cluster:ReadData"],
                    "resources": [f"arn:aws:kafka:{scope}:topic/{tail}/{msk.topic}"],
                },
                {
                    "sid": "MskGroup",
                    "effect": "Allow",
                    "actions": ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"],
                    "resources": [f"arn:aws:kafka:{scope}:group/{tail}/{msk.group_prefix}*"],
                },
            ]
        )
    if access.secret_arns:
        secrets = [r for a in access.secret_arns for r in secret_resources(a)]
        statements.extend(
            [
                {
                    "sid": "Secrets",
                    "effect": "Allow",
                    "actions": ["secretsmanager:GetSecretValue"],
                    "resources": secrets,
                },
                {
                    "sid": "SecretsKms",
                    "effect": "Allow",
                    "actions": ["kms:Decrypt"],
                    "resources": ["*"],
                    "condition": [
                        {
                            "test": "StringEquals",
                            "variable": "kms:ViaService",
                            "values": [_join("secretsmanager.", region, ".amazonaws.com")],
                        },
                        {
                            "test": "StringLike",
                            "variable": "kms:EncryptionContext:SecretARN",
                            "values": secrets,
                        },
                    ],
                },
            ]
        )
    statements.extend(
        [
            {
                "sid": "Logs",
                "effect": "Allow",
                "actions": [
                    "logs:CreateLogStream",
                    "logs:DescribeLogStreams",
                    "logs:PutLogEvents",
                ],
                "resources": [access.log_group_arn, _join(access.log_group_arn, ":*")],
            },
            {
                # DescribeLogGroups has no resource-level form.
                "sid": "LogsDescribe",
                "effect": "Allow",
                "actions": ["logs:DescribeLogGroups"],
                "resources": ["*"],
            },
        ]
    )
    if access.glue_vpc:
        # A Glue job in a VPC (its NETWORK connection) creates, tags and removes its
        # workers' network interfaces as its own role, as AWSGlueServiceRole grants it.
        # Tagging and deleting are held to interfaces in the connection's own subnet, with
        # the aws-glue-service-resource tag, so the role cannot tag another team's
        # interface and then delete it; it never removes tags.
        in_subnet = {
            "test": "ArnEquals",
            "variable": "ec2:Subnet",
            "values": [access.glue_subnet_arn],
        }
        statements.extend(
            [
                {
                    "sid": "GlueVpc",
                    "effect": "Allow",
                    "actions": [
                        "ec2:CreateNetworkInterface",
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DescribeRouteTables",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeVpcAttribute",
                        "ec2:DescribeVpcEndpoints",
                        "ec2:DescribeVpcs",
                    ],
                    # EC2 offers no resource-level scope for these.
                    "resources": ["*"],
                },
                {
                    "sid": "GlueVpcTags",
                    "effect": "Allow",
                    "actions": ["ec2:CreateTags"],
                    "resources": ["arn:aws:ec2:*:*:network-interface/*"],
                    "condition": [
                        {
                            "test": "ForAllValues:StringEquals",
                            "variable": "aws:TagKeys",
                            "values": ["aws-glue-service-resource"],
                        },
                        # ForAllValues alone also holds for a request with no tag keys.
                        {"test": "Null", "variable": "aws:TagKeys", "values": ["false"]},
                        in_subnet,
                    ],
                },
                {
                    "sid": "GlueVpcDelete",
                    "effect": "Allow",
                    "actions": ["ec2:DeleteNetworkInterface"],
                    "resources": ["arn:aws:ec2:*:*:network-interface/*"],
                    "condition": [
                        {
                            "test": "Null",
                            "variable": "aws:ResourceTag/aws-glue-service-resource",
                            "values": ["false"],
                        },
                        in_subnet,
                    ],
                },
            ]
        )
    return {"statement": statements}


def trust_document(
    runtime: str,
    *,
    account: Any,
    region: Any,
    role_name: str,
    application_id: Any = None,
    eks_issuer: Any = None,
    eks_namespace: Any = None,
    glue_job: Any = None,
) -> Dict[str, Any]:
    """The ``assume_role_policy`` document for ``runtime`` (not ``emr-ec2``: no role)."""
    if runtime == "emr-serverless":
        app_arn = _join(
            "arn:aws:emr-serverless:", region, ":", account, ":/applications/", application_id
        )
        return {
            "statement": [
                {
                    "effect": "Allow",
                    "actions": ["sts:AssumeRole"],
                    "principals": [
                        {"type": "Service", "identifiers": ["emr-serverless.amazonaws.com"]}
                    ],
                    "condition": [
                        {
                            "test": "StringEquals",
                            "variable": "aws:SourceAccount",
                            "values": [account],
                        },
                        {
                            "test": "ArnLike",
                            "variable": "aws:SourceArn",
                            "values": [
                                app_arn,
                                _join(app_arn, "/*"),
                            ],
                        },
                    ],
                }
            ]
        }
    if runtime == "glue":
        condition = [{"test": "StringEquals", "variable": "aws:SourceAccount", "values": [account]}]
        if glue_job is not None:
            # Only the build's own Glue job may assume it (the confused-deputy guard).
            condition.append(
                {
                    "test": "ArnLike",
                    "variable": "aws:SourceArn",
                    "values": [_join("arn:aws:glue:", region, ":", account, ":job/", glue_job)],
                }
            )
        return {
            "statement": [
                {
                    "effect": "Allow",
                    "actions": ["sts:AssumeRole"],
                    "principals": [{"type": "Service", "identifiers": ["glue.amazonaws.com"]}],
                    "condition": condition,
                }
            ]
        }
    if runtime == "emr-eks":
        return {
            "statement": [
                {
                    "effect": "Allow",
                    "actions": ["sts:AssumeRoleWithWebIdentity"],
                    "principals": [
                        {
                            "type": "Federated",
                            "identifiers": [
                                _join("arn:aws:iam::", account, ":oidc-provider/", eks_issuer)
                            ],
                        }
                    ],
                    "condition": [
                        {
                            "test": "StringLike",
                            "variable": _join(eks_issuer, ":sub"),
                            "values": [
                                _join(
                                    "system:serviceaccount:",
                                    eks_namespace,
                                    ":emr-containers-sa-*-*-",
                                    account,
                                    "-",
                                    base36(role_name),
                                )
                            ],
                        }
                    ],
                }
            ]
        }
    raise ValueError(f"no forge-owned role for runtime {runtime!r}")


# --- the scheduler that starts the runs (fluid spark run on MWAA) ----------------------

#: The service a run's role is passed to, where StartJobRun passes it (the runtimes in
#: the runs lane's ``PASS_ROLE``).
PASSED_TO = {
    "emr-serverless": "emr-serverless.amazonaws.com",
    "emr-eks": "emr-containers.amazonaws.com",
}
#: Granted only where a run finds an isolated application by its name.
_FIND_APPLICATION = "emr-serverless:ListApplications"


def caller_actions(runtime: str) -> List[str]:
    """What starting, watching and stopping a run takes on ``runtime``: the runs lane's own
    list (``build_runners/spark/_adapters.py::CALLER_ACTIONS``), so the two cannot drift."""
    from fluid_build.build_runners.spark._adapters import CALLER_ACTIONS

    return sorted(a for a in CALLER_ACTIONS[runtime] if a != _FIND_APPLICATION)


def passes_role(runtime: str) -> bool:
    from fluid_build.build_runners.spark._adapters import PASS_ROLE

    return runtime in PASS_ROLE


@dataclass(frozen=True)
class CallerRun:
    """One scheduled build, as the scheduler's policy names it."""

    sid: str
    runtime: str
    target: Any  # the application id, Glue job name, cluster id or virtual cluster id
    role_arn: Any
    objects: Tuple[Any, ...]
    #: An isolated EMR Serverless application is found by name: ListApplications.
    lookup_application: bool = False


def caller_document(runs: Sequence[CallerRun], *, account: Any, region: Any) -> Dict[str, Any]:
    """The least-privilege ``aws_iam_policy_document`` of whatever starts the runs."""
    statements: List[Dict[str, Any]] = []
    for run in runs:
        scope = (region, ":", account, ":")
        if run.runtime == "emr-serverless":
            base = _join("arn:aws:emr-serverless:", *scope, "/applications/", run.target)
            targets = [base, _join(base, "/jobruns/*")]
        elif run.runtime == "glue":
            targets = [_join("arn:aws:glue:", *scope, "job/", run.target)]
        elif run.runtime == "emr-ec2":
            targets = [_join("arn:aws:elasticmapreduce:", *scope, "cluster/", run.target)]
        else:
            base = _join("arn:aws:emr-containers:", *scope, "/virtualclusters/", run.target)
            targets = [base, _join(base, "/jobruns/*")]
        statements.append(
            {
                "sid": f"Run{run.sid}",
                "effect": "Allow",
                "actions": caller_actions(run.runtime),
                "resources": targets,
            }
        )
        if run.lookup_application:
            statements.append(
                {
                    # No resource-level form: how a run finds an application by its name.
                    "sid": f"FindApplication{run.sid}",
                    "effect": "Allow",
                    "actions": [_FIND_APPLICATION],
                    "resources": ["*"],
                }
            )
        if passes_role(run.runtime) and run.role_arn is not None:
            statements.append(
                {
                    "sid": f"PassRole{run.sid}",
                    "effect": "Allow",
                    "actions": ["iam:PassRole"],
                    "resources": [run.role_arn],
                    "condition": [
                        {
                            "test": "StringEquals",
                            "variable": "iam:PassedToService",
                            "values": [PASSED_TO[run.runtime]],
                        }
                    ],
                }
            )
        statements.append(
            {
                "sid": f"Artifacts{run.sid}",
                "effect": "Allow",
                "actions": ["s3:GetObject"],
                "resources": list(run.objects),
            }
        )
    return {"statement": statements}


# --- the Lake Formation registration role of a location a Spark job writes -------------


def registration_trust(account: Any) -> Dict[str, Any]:
    """Lake Formation assumes the role to vend credentials for the registered location."""
    return {
        "statement": [
            {
                "effect": "Allow",
                "actions": ["sts:AssumeRole"],
                "principals": [{"type": "Service", "identifiers": ["lakeformation.amazonaws.com"]}],
                "condition": [
                    {"test": "StringEquals", "variable": "aws:SourceAccount", "values": [account]}
                ],
            }
        ]
    }


def registration_document(bucket: Any, prefix: Any, *, region: Any) -> Dict[str, Any]:
    """Read and write on the registered prefix: what the credentials Lake Formation vends
    to a writer (EMR 7.12 writes Iceberg with them) can do. The service-linked role has no
    ``s3:PutObject``, so a write through Lake Formation needs a role like this one."""
    bucket_arn = _join("arn:aws:s3:::", bucket)
    objects = _join(bucket_arn, "/", prefix, "*")
    return {
        "statement": [
            {
                "sid": "ReadWrite",
                "effect": "Allow",
                "actions": [
                    "s3:AbortMultipartUpload",
                    "s3:DeleteObject",
                    "s3:GetObject",
                    "s3:PutObject",
                ],
                "resources": [objects],
            },
            {
                "sid": "List",
                "effect": "Allow",
                "actions": ["s3:ListBucket"],
                "resources": [bucket_arn],
                "condition": [
                    {"test": "StringLike", "variable": "s3:prefix", "values": [_join(prefix, "*")]}
                ],
            },
            {
                "sid": "S3Kms",
                "effect": "Allow",
                "actions": ["kms:Decrypt", "kms:GenerateDataKey"],
                "resources": ["*"],
                "condition": [
                    {
                        "test": "StringEquals",
                        "variable": "kms:ViaService",
                        "values": [_join("s3.", region, ".amazonaws.com")],
                    },
                    {
                        "test": "StringLike",
                        "variable": "kms:EncryptionContext:aws:s3:arn",
                        "values": [bucket_arn, objects],
                    },
                ],
            },
        ]
    }
