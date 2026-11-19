# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a Spark build's run should be: its job config, its runtime and its config hash.

One :class:`DesiredRun` per ``engine: spark`` build, from the contract and the operator's
environment, the same at ``fluid plan`` and ``fluid apply``. Its ``config_hash`` is the
sha256 of the job config forge generates, the runner's own sha256 and the settings the run
is started with (runtime, target, release, role, Spark configuration, retries, and on Glue
the pinned jars it loads). A running job whose hash differs is replaced; one whose hash
matches is left alone.

``source_id`` and ``checkpoint_id`` say which stream a streaming job reads and where it
keeps its position. A checkpoint holds the offsets of one topic on one cluster, so the
reconcile refuses a new source on the old checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.spark import names as _names

#: The action ``fluid plan`` lists for each Spark build; ``fluid apply`` reconciles it.
ACTION = "spark.reconcile"
#: The AWS account in a run's names until it is started: plans, hashes and the bound-plan
#: check never need credentials, and a plan made anywhere matches the apply's.
ACCOUNT_TOKEN = "${aws_account_id}"
#: The region (and its partition) of a build whose output declares none, likewise.
REGION_TOKEN = "${aws_region}"
PARTITION_TOKEN = "${aws_partition}"
#: Where a local run's artifacts live (``FLUID_SPARK_LOCAL_ROOT``, read when the run is
#: started), likewise: a plan made on one machine matches the apply on another.
LOCAL_ROOT_TOKEN = "${fluid_spark_local_root}"
#: ``maxFailedAttemptsPerHour`` of an EMR Serverless streaming job when the build sets no
#: ``execution.retries.maxAttempts``: AWS's own default. AWS allows 1 to 10.
DEFAULT_STREAMING_ATTEMPTS_PER_HOUR = 5
_MAX_STREAMING_ATTEMPTS_PER_HOUR = 10

# EMR 7.x paths and the Maven coordinates the runner's docstring lists (Spark 3.5).
_EMR_ICEBERG_JAR = "/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar"
_ICEBERG_EXTENSIONS = "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"
_KAFKA_PACKAGE = "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1"
_MSK_IAM_PACKAGE = "software.amazon.msk:aws-msk-iam-auth:2.2.0"
_AVRO_PACKAGES = ("org.apache.spark:spark-avro_2.12:3.5.1", "za.co.absa:abris_2.12:6.4.0")
_CONFLUENT_REPOSITORY = "https://packages.confluent.io/maven/"
#: Operator-only: one https Maven repository (a CodeArtifact or Artifactory mirror that
#: proxies Maven Central and Confluent's repository) a run resolves its packages from,
#: in place of :data:`_CONFLUENT_REPOSITORY`. Unset: Maven Central, and Confluent's
#: repository for Avro. The cluster needs egress to the one it uses.
MAVEN_REPOSITORY_ENV = "FLUID_SPARK_MAVEN_REPOSITORY"

#: Operator-only: Spark settings a contract's ``sparkConf`` may set beyond the tuning ones,
#: comma-separated names or ``prefix*`` (``spark.sql.streaming.*``). Unset: tuning only.
CONF_ALLOWLIST_ENV = "FLUID_SPARK_CONF_ALLOWLIST"
#: Resource and tuning settings, which load nothing and set no environment.
_CONF_TUNING = frozenset(
    {
        "spark.executor.memory",
        "spark.executor.cores",
        "spark.executor.instances",
        "spark.executor.memoryOverhead",
        "spark.executor.memoryOverheadFactor",
        "spark.driver.memory",
        "spark.driver.cores",
        "spark.driver.memoryOverhead",
        "spark.driver.maxResultSize",
        "spark.default.parallelism",
        "spark.sql.shuffle.partitions",
        "spark.sql.files.maxPartitionBytes",
        "spark.sql.autoBroadcastJoinThreshold",
        "spark.network.timeout",
        "spark.task.maxFailures",
        "spark.speculation",
        "spark.emr-serverless.driver.disk",
        "spark.emr-serverless.executor.disk",
        "spark.emr-serverless.memoryOverheadFactor",
    }
)
_CONF_TUNING_PREFIXES = ("spark.dynamicAllocation.", "spark.sql.adaptive.")
#: Never from a contract unless the operator lists the exact key or a prefix of it: they
#: load code, pick classes, or set the job's environment.
_CONF_DENIED = frozenset(
    {
        "spark.archives",
        "spark.files",
        "spark.plugins",
        # Under the adaptive prefix, but it names a class Spark loads.
        "spark.sql.adaptive.customCostEvaluatorClass",
        "spark.sql.streaming.stateStore.providerClass",
        # Move what the job writes, or switch off what the runner relies on.
        "spark.sql.defaultCatalog",
        "spark.sql.warehouse.dir",
        "spark.sql.streaming.checkpointLocation",
        "spark.sql.streaming.streamingQueryListeners",
        "spark.sql.variable.substitute",
        "spark.sql.session.timeZone",
        "spark.sql.mapKeyDedupPolicy",
        "spark.driver.userClassPathFirst",
        "spark.executor.userClassPathFirst",
        "spark.driver.defaultJavaOptions",
        "spark.executor.defaultJavaOptions",
        "spark.authenticate",
        "spark.master",
    }
)
#: The schema's sparkConf key names; checked here too, for a caller that skips the schema.
_CONF_KEY = re.compile(r"[A-Za-z0-9_.-]{1,256}")
#: With :data:`_CONF_DENIED`, at least the infrastructure emit's own list
#: (``iac/providers/aws_identity.py::DENIED_SPARK_CONF``; a test checks the two agree).
_CONF_DENIED_PREFIXES = (
    "spark.jars",
    "spark.files.",
    "spark.plugins.",
    "spark.pyspark.",
    "spark.python.",
    "spark.emr-serverless.executorEnv.",
    "spark.emr-serverless.lakeformation.",
    "spark.kerberos.",
    "spark.security.",
    "spark.authenticate.",
    "spark.ssl.",
    "spark.redaction.",
    "spark.eventLog.",
    "spark.ui.",
    "spark.app.",
    "spark.submit.",
    "spark.sql.extensions",
    "spark.sql.catalog",
    "spark.sql.queryExecutionListeners",
    "spark.extraListeners",
    "spark.hadoop.",
    "spark.executorEnv.",
    "spark.yarn.",
    "spark.kubernetes.",
    "spark.emr-serverless.driverEnv.",
    "spark.driver.extra",
    "spark.executor.extra",
)


def _sha(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DesiredRun:
    """One Spark build's run, as ``fluid apply`` starts it (a stream after ``tofu apply``, a
    scheduled build in its build phase)."""

    product: str
    build: str
    runtime: str
    mode: str
    #: True for a batch job and for a stream with ``trigger.availableNow``: a run that
    #: ends, started on a schedule (``fluid apply --mode amend-and-build``), never kept running.
    scheduled: bool
    region: str
    config_hash: str
    job_digest: str
    runner_sha256: str
    job_uri: str
    runner_uri: str
    run_name: str
    role_arn: str
    target: Mapping[str, str]
    release_label: Optional[str]
    glue_version: Optional[str]
    spark_conf: Mapping[str, str]
    retry_attempts: Optional[int]
    source_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    checkpoint: Optional[str] = None
    topic: Optional[str] = None
    job_text: str = field(default="", repr=False, compare=False)

    @property
    def long_running(self) -> bool:
        return not self.scheduled

    @property
    def needs_account(self) -> bool:
        return any(ACCOUNT_TOKEN in v for v in (self.job_uri, self.runner_uri, self.role_arn))

    @property
    def needs_region(self) -> bool:
        return self.region == REGION_TOKEN

    @property
    def local(self) -> bool:
        """A run of ``runtime: local``: spark-submit on the machine ``fluid apply`` runs on."""
        return self.runtime == "local"

    @property
    def needs_local_root(self) -> bool:
        return LOCAL_ROOT_TOKEN in self.job_uri  # the runner's URI has the same root

    def bind(
        self,
        account: Optional[str] = None,
        region: Optional[str] = None,
        local_root: Optional[str] = None,
    ) -> "DesiredRun":
        """This run with the account, region and local artifact root its names hold
        (:data:`ACCOUNT_TOKEN`, :data:`REGION_TOKEN`, :data:`LOCAL_ROOT_TOKEN`); the hash
        stays."""
        import dataclasses

        def sub(value: str) -> str:
            if account:
                value = value.replace(ACCOUNT_TOKEN, account)
            if region:
                value = value.replace(PARTITION_TOKEN, _names.partition(region))
            if local_root:
                value = value.replace(LOCAL_ROOT_TOKEN, local_root.rstrip("/"))
            return value

        return dataclasses.replace(
            self,
            region=region if region and self.needs_region else self.region,
            job_uri=sub(self.job_uri),
            runner_uri=sub(self.runner_uri),
            role_arn=sub(self.role_arn),
            checkpoint=sub(self.checkpoint) if self.checkpoint else self.checkpoint,
        )

    def identity_args(self) -> List[str]:
        """The flags that mark a run as this build's, read back from any runtime.

        The runner ignores what is not its own (``parse_known_args``)."""
        args = ["--fluid-config-hash", self.config_hash]
        if self.source_id:
            args += ["--fluid-source", self.source_id]
        if self.checkpoint_id:
            args += ["--fluid-checkpoint", self.checkpoint_id]
        return args

    def entry_args(self) -> List[str]:
        return ["--config", self.job_uri, *self.identity_args()]

    def tags(self) -> Dict[str, str]:
        tags = {
            _names.PRODUCT_TAG: self.product,
            _names.BUILD_TAG: self.build,
            "fluid_config_hash": self.config_hash,
        }
        if self.source_id:
            tags["fluid_source"] = self.source_id
        if self.checkpoint_id:
            tags["fluid_checkpoint"] = self.checkpoint_id
        return tags

    def plan_action(self, step: int) -> Dict[str, Any]:
        """The plan's ``spark.reconcile`` action, with ``op`` and ``action_type`` both set:
        apply's dispatcher reads ``op``, display reads ``action_type``."""
        return {
            "step": step,
            "action_id": f"spark_{self.build}",
            "provider": "local" if self.local else "aws",
            "depends_on": [],
            "op": ACTION,
            "action_type": ACTION,
            "id": self.build,
            "payload": {
                "product": self.product,
                "build": self.build,
                "runtime": self.runtime,
                "mode": self.mode,
                "scheduled": self.scheduled,
                "region": self.region,
                "target": dict(self.target),
                "executionRoleArn": self.role_arn or None,
                "artifacts": {"jobConfig": self.job_uri, "runner": self.runner_uri},
                "jobConfigDigest": self.job_digest,
                "runnerSha256": self.runner_sha256,
                "configHash": self.config_hash,
                "sourceId": self.source_id,
                "checkpointId": self.checkpoint_id,
                "stateImpact": (
                    "started by a scheduler (fluid apply --mode amend-and-build); a plain "
                    "fluid apply does not run it"
                    if self.scheduled
                    else "a changed config cancels the running job and starts it again on "
                    "the same checkpoint; a new source on that checkpoint is refused"
                ),
            },
        }


def _retry_attempts(build: Mapping[str, Any]) -> Optional[int]:
    retries = (build.get("execution") or {}).get("retries")
    attempts = retries.get("maxAttempts") if isinstance(retries, Mapping) else None
    return attempts if isinstance(attempts, int) and attempts >= 1 else None


def _submit_conf(
    runtime: str,
    config: Mapping[str, Any],
    user: Mapping[str, Any],
    where: str = "Spark build",
    env: Optional[Mapping[str, str]] = None,
    policy: bool = True,
) -> Dict[str, str]:
    """The Spark configuration a run is submitted with: what the runner needs on the class
    path (its docstring lists it), then the build's allowed ``sparkConf`` (:func:`_allowed_conf`).
    """
    env = os.environ if env is None else env
    conf: Dict[str, str] = {}
    if runtime != "glue":  # Glue's job carries --datalake-formats iceberg and its jars
        conf["spark.jars"] = _EMR_ICEBERG_JAR
        conf["spark.sql.extensions"] = _ICEBERG_EXTENSIONS
        source = config.get("source")
        if isinstance(source, Mapping):
            packages = [_KAFKA_PACKAGE]
            if source.get("auth") == "msk-iam":
                packages.append(_MSK_IAM_PACKAGE)
            formats = {
                (source.get(part) or {}).get("format")
                for part in ("value", "key")
                if isinstance(source.get(part), Mapping)
            }
            if "avro-confluent" in formats:
                packages.extend(_AVRO_PACKAGES)
                conf["spark.jars.repositories"] = _CONFLUENT_REPOSITORY
            mirror = maven_repository(env)
            if mirror:
                conf["spark.jars.repositories"] = mirror
            conf["spark.jars.packages"] = ",".join(packages)
    if policy:
        conf.update(_allowed_conf(user, where, env))
    else:  # planning: the operator's list is apply's to check
        conf.update({str(k): str(v) for k, v in user.items()})
    return conf


def maven_repository(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The operator's Maven mirror (:data:`MAVEN_REPOSITORY_ENV`), or ``None`` when unset.

    One https URL with a host, no user or password, no query or fragment, and no comma
    (``spark.jars.repositories`` is a list). A link-local or metadata host is refused. A
    credential never goes in the URL: use a mirror the job's role reaches with IAM, such
    as CodeArtifact through a VPC endpoint. The variable is protected, so a project's
    ``.env`` cannot set it.

    Raises:
        SparkPolicyError: a URL that breaks one of those rules (the message names the
            rule, never the URL).
    """
    from urllib.parse import urlsplit

    from fluid_build._errors import doc_url
    from fluid_build._net import _hostname_is_link_local
    from fluid_build.spark.policy import SparkPolicyError

    env = os.environ if env is None else env
    raw = (env.get(MAVEN_REPOSITORY_ENV) or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        parts, host = None, None
    if parts is None or parts.scheme.lower() != "https" or not host:
        problem = "is not an https URL with a host"
    elif parts.username is not None or parts.password is not None or "@" in parts.netloc:
        problem = "carries a user or password"
    elif any(c in raw for c in "?#,") or any(c.isspace() for c in raw):
        problem = "has a query, a fragment, a comma or a space"
    elif host.endswith(".") or any(c in parts.netloc for c in "%\\") or parts.port == 0:
        # a host spelt so it does not resolve here may still resolve on the cluster
        problem = "has a host ending in a dot, a % or \\ in it, or port 0"
    elif not host.isascii():  # an IDN dot (U+3002) would slip past the dot check
        problem = "has a host that is not a valid DNS name (spell an IDN in punycode)"
    else:
        try:
            link_local = _hostname_is_link_local(host)
        except ValueError:  # IDNA: an empty or over-long label
            link_local, problem = None, "has a host that is not a valid DNS name"
        else:
            problem = "names a link-local or metadata address"
        if link_local is False:
            return raw
    raise SparkPolicyError(
        what=f"{MAVEN_REPOSITORY_ENV} {problem}",
        why=(
            "A Spark run resolves its packages from this repository, so it must be one "
            "https mirror, reached without a credential in the URL."
        ),
        fix=(
            f"Set {MAVEN_REPOSITORY_ENV} to one https URL such as "
            "https://<domain>-<account>.d.codeartifact.<region>.amazonaws.com/maven/<repo>/, "
            "reached with the job role's IAM permissions, or unset it."
        ),
        doc=doc_url(),
    )


def _conf_denied(name: str) -> bool:
    return name in _CONF_DENIED or name.startswith(_CONF_DENIED_PREFIXES)


def _conf_allowed(name: str, extra: Sequence[str]) -> bool:
    if _conf_denied(name):  # absolute: no operator entry admits one, as in the IaC emit
        return False
    if name in _CONF_TUNING or name.startswith(_CONF_TUNING_PREFIXES):
        return True
    return name in extra or any(e.endswith("*") and name.startswith(e[:-1]) for e in extra)


def _allowed_conf(user: Mapping[str, Any], where: str, env: Mapping[str, str]) -> Dict[str, str]:
    """The build's ``sparkConf``, if every setting is one a contract may make.

    A Spark setting can load code into the job (``spark.jars*``, ``spark.sql.extensions``,
    ``spark.plugins``, an ``extraJavaOptions`` agent, a catalog implementation) or hand it
    environment variables; that code runs with the job's role, which reads the whole Glue
    catalog, and would get round the contract-SQL allowlist and the host allowlist. So a
    contract sets resource and tuning settings only (:data:`_CONF_TUNING`); the operator
    lists any other key in :data:`CONF_ALLOWLIST_ENV` (a name, or a prefix ending in ``*``).
    The denied keys (:data:`_CONF_DENIED`, :data:`_CONF_DENIED_PREFIXES`) are refused even
    when listed, by name or by a prefix such as ``spark.sql.*``: the infrastructure emit
    never passes them either, so a run is no looser than what ``fluid apply`` provisions.
    A value holds no whitespace: Glue joins the settings into one ``--conf`` argument.

    Raises:
        SparkPolicyError: a setting the operator has not allowed, or a denied one.
    """
    from fluid_build._errors import doc_url
    from fluid_build.spark.policy import SparkPolicyError

    extra = [e.strip() for e in (env.get(CONF_ALLOWLIST_ENV) or "").split(",") if e.strip()]
    out: Dict[str, str] = {}
    refused: Dict[str, str] = {}  # name -> why: "name", "denied", "unlisted" or "value"
    for key, value in user.items():
        name, text = str(key), str(value)
        if not _CONF_KEY.fullmatch(name):
            refused[name] = "name"
        elif _conf_denied(name):
            refused[name] = "denied"
        elif not _conf_allowed(name, extra):
            refused[name] = "unlisted"
        elif not text or any(c.isspace() for c in text):
            refused[name] = "value"
        else:
            out[name] = text
    if refused:
        shown = ", ".join(_conf_name(n) for n in sorted(refused))
        raise SparkPolicyError(
            what=f"{where}: parameters.spark.sparkConf sets {shown}",
            why=(
                "A contract sets Spark resource and tuning settings only (executor and driver "
                "size, dynamic allocation, shuffle partitions, adaptive execution), with values "
                "without spaces. Other settings can load code or environment into the job, "
                "which runs with the job's role and reads what that role can read."
            ),
            fix=_conf_fix(refused),
            doc=doc_url(),
        )
    return out


def _conf_name(name: str) -> str:
    """A key as the refusal shows it: quoted when it is not a valid name, so a trailing
    space or a newline is visible."""
    return name if _CONF_KEY.fullmatch(name) else repr(name)


def _conf_fix(refused: Mapping[str, str]) -> str:
    """One hint per reason; only a valid, undenied, unlisted key gets the ``export`` hint,
    the one case the operator's list can change."""

    def names(why: str) -> List[str]:
        return [_conf_name(n) for n in sorted(refused) if refused[n] == why]

    parts = []
    if names("name"):
        parts.append(
            f"Fix the key {', '.join(names('name'))}: a Spark setting name holds letters, "
            "digits, '_', '.' and '-' only, with no spaces."
        )
    if names("denied"):
        parts.append(
            f"Drop {', '.join(names('denied'))}: forge never passes it, and no "
            f"{CONF_ALLOWLIST_ENV} entry admits it."
        )
    if names("value"):
        parts.append(f"Give {', '.join(names('value'))} a value without spaces.")
    if names("unlisted"):
        parts.append(
            f"Drop {', '.join(names('unlisted'))}, or, once it is reviewed, have the operator "
            f'list it in the real environment: export {CONF_ALLOWLIST_ENV}="'
            f'{",".join(names("unlisted"))}".'
        )
    return " ".join(parts)


def resolve_region(environ: Optional[Mapping[str, str]] = None) -> str:
    """The region of the default AWS credentials: AWS_REGION, AWS_DEFAULT_REGION, the profile."""
    env = os.environ if environ is None else environ
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if not region:
        try:
            import boto3

            region = boto3.session.Session().region_name
        except ImportError:
            region = None
    if not region:
        raise ValueError(
            "a Spark build's output declares no binding.location.region and the AWS "
            "credentials name none; set AWS_REGION or the output's region"
        )
    return str(region)


def resolve_account(region: str) -> str:
    """The account the default AWS credentials are in, as the AWS provider resolves it."""
    from fluid_build.providers.aws.util.config import (
        account_unresolved_message,
        resolve_account_and_region,
    )

    resolved, _ = resolve_account_and_region(None, region)
    if not resolved:
        raise ValueError(account_unresolved_message())
    return resolved


def spark_builds(contract: Mapping[str, Any], build_id: Optional[str] = None) -> List[Any]:
    from fluid_build.spark.compiler import spark_builds as _builds
    from fluid_build.streaming.compiler import managed_lane_enabled

    if not managed_lane_enabled(contract):
        return []
    return [b for b in _builds(contract) if build_id is None or b.get("id") == build_id]


def _placement(
    cfg: Mapping[str, Any], runtime: str, product: str, bid: str, loc: Mapping[str, Any]
) -> Tuple[str, str, Dict[str, str]]:
    """Where a run of the build runs: its region, the role it runs as, and its target (the
    application, cluster, virtual cluster or Glue job). A local run has none of them: it
    runs with the credentials of the machine it runs on."""
    if runtime == "local":
        return "", "", {}
    # An undeclared region is the credentials' when the run starts (REGION_TOKEN), so a
    # plan and its apply agree whatever region each shell has.
    region = str(loc.get("region") or REGION_TOKEN)
    partition = PARTITION_TOKEN if region == REGION_TOKEN else _names.partition(region)
    role = str(
        cfg.get("executionRoleArn")
        or f"arn:{partition}:iam::{ACCOUNT_TOKEN}:role/{_names.execution_role_name(product, bid)}"
    )
    target: Dict[str, str] = {}
    if runtime == "emr-serverless":
        if cfg.get("applicationId"):
            target["applicationId"] = str(cfg["applicationId"])
        else:
            target["applicationName"] = _names.application_name(product)
    elif runtime == "emr-ec2":
        if not cfg.get("clusterId"):
            raise ValueError(
                f"Spark build {bid}: runtime emr-ec2 runs on a cluster that is running; "
                "set parameters.spark.clusterId (a transient cluster is not supported yet)"
            )
        target["clusterId"] = str(cfg["clusterId"])
    elif runtime == "emr-eks":
        target["virtualClusterId"] = str(cfg["virtualClusterId"])
    elif runtime == "glue":
        target["glueJobName"] = _names.glue_job_name(product, bid)
    return region, role, target


def _attempts(build: Mapping[str, Any], runtime: str, scheduled: bool) -> Optional[int]:
    """The run's retry policy. A local run has none: fluid apply starts a stream stopped
    from outside forge again, and reports one that failed until the build changes."""
    if runtime == "local":
        return None
    attempts = _retry_attempts(build)
    if not scheduled and runtime == "emr-serverless":
        attempts = min(
            attempts or DEFAULT_STREAMING_ATTEMPTS_PER_HOUR, _MAX_STREAMING_ATTEMPTS_PER_HOUR
        )
    return attempts


def desired_runs(
    contract: Mapping[str, Any],
    *,
    account: Optional[str] = None,
    build_id: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    policy: bool = True,
) -> List[DesiredRun]:
    """Each Spark build's :class:`DesiredRun`, in contract order (``build_id``: just that one).

    ``account`` binds the runs to that AWS account; when None, a name that needs one (a
    bucketless output, a role forge creates) holds :data:`ACCOUNT_TOKEN` until the run is
    started, so planning needs no credentials. The config hash never depends on it.

    Raises:
        ValueError: a build forge cannot compile, or two builds with one id.
        FluidUserError: the operator's policy refuses a build (``fluid_build.spark.policy``).
    """
    from fluid_build.spark import runner_source
    from fluid_build.spark.compiler import LOCAL_RUNTIME, compile_job_config, spark_parameters

    builds = spark_builds(contract, build_id)
    ids = [b.get("id") for b in builds]
    repeated = sorted({str(i) for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(f"Spark build ids must be unique; {repeated[0]!r} is used twice")
    env = os.environ if environ is None else environ
    runner_sha = _sha(runner_source())
    runs = []
    for build in builds:
        cfg = spark_parameters(build) or {}
        bid = str(build.get("id"))
        product = str(contract.get("id"))
        exposes = {e.get("exposeId"): e for e in contract.get("exposes") or []}
        loc = ((exposes.get((build.get("outputs") or [None])[0]) or {}).get("binding") or {}).get(
            "location"
        ) or {}
        from fluid_build.providers.aws.util.warehouse import (
            normalize_location,
        )

        runtime = str(cfg.get("runtime"))
        local = runtime == LOCAL_RUNTIME
        region, role, target = _placement(cfg, runtime, product, bid, loc)
        acct = ACCOUNT_TOKEN
        job = compile_job_config(
            contract,
            build,
            account_ref=acct,
            environ=env,
            contract_sql_gate=policy,
            host_gate=policy,
        )
        config = job.config
        bucket, _ = normalize_location(loc, account_ref=acct)
        trigger = config.get("trigger") or {}
        scheduled = config["mode"] == "batch" or bool(trigger.get("availableNow"))
        attempts = _attempts(build, runtime, scheduled)
        spark_conf = _submit_conf(
            runtime, config, cfg.get("sparkConf") or {}, f"Spark build {bid}", env, policy
        )
        release = (
            None
            if runtime == LOCAL_RUNTIME
            else (
                str(cfg["releaseLabel"])
                if cfg.get("releaseLabel")
                else _names.default_release(runtime)
            )
        )
        glue_version = str(cfg.get("glueVersion") or "5.0") if runtime == "glue" else None
        source, checkpoint = config.get("source"), config.get("checkpoint")
        source_id = (
            _sha(
                {
                    "topic": source.get("topic"),
                    "cluster": source.get("mskClusterArn")
                    or sorted(str(source.get("bootstrapServers") or "").split(",")),
                }
            )[:16]
            if isinstance(source, Mapping)
            else None
        )
        settings = {
            "runtime": runtime,
            "scheduled": scheduled,
            "target": target,
            "role": role,
            "release": release,
            "glueVersion": glue_version,
            "sparkConf": spark_conf,
            "retry": attempts,
        }
        if runtime == "glue":
            # The pinned jars the Glue job loads (--extra-jars), from the same function and
            # inputs the IaC emit uses: a new pin or prefix restarts a running stream. Absent,
            # not None, for a build that adds none, so its hash stays what it was.
            from fluid_build.iac.providers.aws_glue_jars import glue_jars_fingerprint

            jars = glue_jars_fingerprint(config, env)
            if jars is not None:
                settings["glueJars"] = jars
        # A local run reads its artifacts where the operator keeps them, in the same layout
        # as the S3 objects OpenTofu uploads on AWS (fluid_build/spark/names.py).
        root = LOCAL_ROOT_TOKEN if local else f"s3://{bucket}"
        runs.append(
            DesiredRun(
                product=product,
                build=bid,
                runtime=runtime,
                mode=str(config["mode"]),
                scheduled=scheduled,
                region=region,
                config_hash=_sha({"job": job.digest, "runner": runner_sha, "settings": settings}),
                job_digest=job.digest,
                runner_sha256=runner_sha,
                job_uri=_names.job_config_uri(root, product, bid),
                runner_uri=_names.runner_uri(root, product, bid, runner_sha),
                run_name=_names.run_name(product, bid),
                role_arn=role,
                target=target,
                release_label=release,
                glue_version=glue_version,
                spark_conf=spark_conf,
                retry_attempts=attempts,
                source_id=source_id,
                checkpoint_id=_sha(checkpoint)[:16] if checkpoint else None,
                checkpoint=checkpoint,
                topic=source.get("topic") if isinstance(source, Mapping) else None,
                job_text=job.text,
            )
        )
    return [run.bind(account) for run in runs] if account else runs


def plan_actions(contract: Mapping[str, Any], **kwargs: Any) -> List[Dict[str, Any]]:
    """The ``spark.reconcile`` actions of the contract's Spark builds (steps from 1)."""
    return [run.plan_action(step) for step, run in enumerate(desired_runs(contract, **kwargs), 1)]
