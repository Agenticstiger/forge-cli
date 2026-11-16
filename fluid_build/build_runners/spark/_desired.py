# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a Spark build's run should be: its job config, its runtime and its config hash.

One :class:`DesiredRun` per ``engine: spark`` build, from the contract and the operator's
environment, the same at ``fluid plan`` and ``fluid apply``. Its ``config_hash`` is the
sha256 of the job config forge generates, the runner's own sha256 and the settings the run
is started with (runtime, target, release, role, Spark configuration, retries). A running
job whose hash differs is replaced; one whose hash matches is left alone.

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
from typing import Any, Dict, List, Mapping, Optional, Sequence

from fluid_build.build_runners.spark import _names

#: The action ``fluid plan`` lists for each Spark build; ``fluid apply`` reconciles it.
ACTION = "spark.reconcile"
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
    }
)
#: The schema's sparkConf key names; checked here too, for a caller that skips the schema.
_CONF_KEY = re.compile(r"[A-Za-z0-9_.-]{1,256}")
_CONF_DENIED_PREFIXES = (
    "spark.jars",
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
    """One Spark build's run, as ``fluid apply`` and ``fluid spark run`` start it."""

    product: str
    build: str
    runtime: str
    mode: str
    #: True for a batch job and for a stream with ``trigger.availableNow``: a run that
    #: ends, started on a schedule (Airflow, ``fluid spark run``), never kept running.
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
            "fluid_product": self.product,
            "fluid_build": self.build,
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
            "provider": "aws",
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
                "executionRoleArn": self.role_arn,
                "artifacts": {"jobConfig": self.job_uri, "runner": self.runner_uri},
                "jobConfigDigest": self.job_digest,
                "runnerSha256": self.runner_sha256,
                "configHash": self.config_hash,
                "sourceId": self.source_id,
                "checkpointId": self.checkpoint_id,
                "stateImpact": (
                    "started by a scheduler (fluid spark run); fluid apply does not run it"
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
            conf["spark.jars.packages"] = ",".join(packages)
    conf.update(_allowed_conf(user, where, env))
    return conf


def _conf_allowed(name: str, extra: Sequence[str]) -> bool:
    if name in _CONF_DENIED or name.startswith(_CONF_DENIED_PREFIXES):
        listed = name in extra or any(e.endswith("*") and name.startswith(e[:-1]) for e in extra)
        return listed
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
    A value holds no whitespace: Glue joins the settings into one ``--conf`` argument.

    Raises:
        SparkPolicyError: a setting the operator has not allowed.
    """
    from fluid_build._errors import doc_url
    from fluid_build.spark.policy import SparkPolicyError

    extra = [e.strip() for e in (env.get(CONF_ALLOWLIST_ENV) or "").split(",") if e.strip()]
    out: Dict[str, str] = {}
    refused = []
    for key, value in user.items():
        name, text = str(key), str(value)
        if (
            not _CONF_KEY.fullmatch(name)
            or not _conf_allowed(name, extra)
            or not text
            or any(c.isspace() for c in text)
        ):
            refused.append(name)
        else:
            out[name] = text
    if refused:
        raise SparkPolicyError(
            what=f"{where}: parameters.spark.sparkConf sets {', '.join(sorted(refused))}",
            why=(
                "A contract sets Spark resource and tuning settings only (executor and driver "
                "size, dynamic allocation, shuffle partitions, adaptive execution), with values "
                "without spaces. Other settings can load code or environment into the job, "
                "which runs with the job's role and reads what that role can read."
            ),
            fix=(
                "Drop the setting, or, once it is reviewed, have the operator list it in the "
                f"real environment: export {CONF_ALLOWLIST_ENV}=\"{','.join(sorted(refused))}\"."
            ),
            doc=doc_url(),
        )
    return out


def _account(account: Optional[str], region: str) -> str:
    if account:
        return account
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


def desired_runs(
    contract: Mapping[str, Any],
    *,
    account: Optional[str] = None,
    build_id: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> List[DesiredRun]:
    """Each Spark build's :class:`DesiredRun`, in contract order (``build_id``: just that one).

    ``account`` is the AWS account the job runs in; when None and a name needs it (a
    bucketless output, a role forge creates) it is resolved as the AWS provider does.

    Raises:
        ValueError: a build forge cannot compile, or two builds with one id.
        FluidUserError: the operator's policy refuses a build (``fluid_build.spark.policy``).
    """
    from fluid_build.spark import runner_source
    from fluid_build.spark.compiler import compile_job_config, spark_parameters

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
            bucket_uses_fallback,
            normalize_location,
        )

        region = str(
            loc.get("region") or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or ""
        )
        if not region:
            raise ValueError(
                f"Spark build {bid}: no AWS region; set the output's binding.location.region "
                "or AWS_REGION"
            )
        needs_account = bucket_uses_fallback(loc) or not cfg.get("executionRoleArn")
        acct = _account(account, region) if needs_account else (account or "")
        job = compile_job_config(contract, build, account_ref=acct, environ=env)
        config = job.config
        bucket, _ = normalize_location(loc, account_ref=acct)
        runtime = str(cfg["runtime"])
        trigger = config.get("trigger") or {}
        scheduled = config["mode"] == "batch" or bool(trigger.get("availableNow"))
        role = cfg.get("executionRoleArn") or (
            f"arn:{_names.partition(region)}:iam::{acct}:role/"
            f"{_names.execution_role_name(product, bid)}"
        )
        target: Dict[str, str] = {}
        if runtime == "emr-serverless":
            if cfg.get("applicationId"):
                target["applicationId"] = str(cfg["applicationId"])
            else:
                target["applicationName"] = _names.application_name(contract)
        elif runtime == "emr-ec2":
            if not cfg.get("clusterId"):
                raise ValueError(
                    f"Spark build {bid}: runtime emr-ec2 runs on a cluster that is running; "
                    "set parameters.spark.clusterId (a transient cluster is not supported yet)"
                )
            target["clusterId"] = str(cfg["clusterId"])
        elif runtime == "emr-eks":
            target["virtualClusterId"] = str(cfg["virtualClusterId"])
        else:
            target["glueJobName"] = _names.glue_job_name(product, bid)
        attempts = _retry_attempts(build)
        if not scheduled and runtime == "emr-serverless":
            attempts = min(
                attempts or DEFAULT_STREAMING_ATTEMPTS_PER_HOUR, _MAX_STREAMING_ATTEMPTS_PER_HOUR
            )
        spark_conf = _submit_conf(
            runtime, config, cfg.get("sparkConf") or {}, f"Spark build {bid}", env
        )
        release = None if runtime == "glue" else str(cfg.get("releaseLabel") or "emr-7.12.0")
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
                job_uri=_names.job_config_uri(bucket, product, bid),
                runner_uri=_names.runner_uri(bucket, runner_sha),
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
    return runs


def plan_actions(contract: Mapping[str, Any], **kwargs: Any) -> List[Dict[str, Any]]:
    """The ``spark.reconcile`` actions of the contract's Spark builds (steps from 1)."""
    return [run.plan_action(step) for step, run in enumerate(desired_runs(contract, **kwargs), 1)]
