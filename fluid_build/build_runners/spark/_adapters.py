# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One adapter per Spark runtime: find a build's runs, start one, read one, cancel one.

| runtime | start | find | cancel |
|---|---|---|---|
| emr-serverless | ``StartJobRun`` (``mode`` ``STREAMING`` / ``BATCH``, ``retryPolicy``) | ``ListJobRuns`` by name, then ``GetJobRun`` for tags | ``CancelJobRun`` |
| glue | ``StartJobRun`` on the build's Glue job, with run ``Arguments`` | ``GetJobRuns`` | ``BatchStopJobRun`` |
| emr-ec2 | ``AddJobFlowSteps`` (``command-runner.jar spark-submit``) on ``clusterId`` | ``ListSteps`` by name | ``CancelSteps`` (``SEND_INTERRUPT``) |
| emr-eks | emr-containers ``StartJobRun`` on ``virtualClusterId`` | ``ListJobRuns`` by name | ``CancelJobRun`` |
| local | ``spark-submit`` on this machine, under a detached supervisor (:mod:`._local`) | the build's run directories under ``.fluid/<product>/runtime/spark/`` | SIGTERM, then SIGKILL, to the run's process group |

Every run carries its build's identity in the runner's arguments
(:meth:`DesiredRun.identity_args`), and in tags where the API takes them, so one rule finds
it on every runtime. States are normalised to :data:`PENDING`, :data:`RUNNING`,
:data:`CANCELLING` and the terminal :data:`SUCCEEDED`, :data:`FAILED`, :data:`CANCELLED`;
the state sets follow Apache Airflow's Amazon provider (``EmrServerlessHook.JOB_*_STATES``,
``EmrContainerHook``; Apache-2.0) and the AWS API references.

boto3 is the ``[aws]`` extra and is imported only by :func:`default_clients`, through
:mod:`fluid_build.util.s3`, which holds the S3 helpers these share with other commands.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from fluid_build.build_runners.spark._desired import RESTART_FLAG, STOP_KEY_FLAG, DesiredRun
from fluid_build.spark import names as _names
from fluid_build.util import s3 as _s3
from fluid_build.util.s3 import error_code as _error_code
from fluid_build.util.s3 import is_denied_code, is_missing_code

PENDING, RUNNING, CANCELLING = "PENDING", "RUNNING", "CANCELLING"
SUCCEEDED, FAILED, CANCELLED = "SUCCEEDED", "FAILED", "CANCELLED"
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})
ACTIVE = frozenset({PENDING, RUNNING, CANCELLING})

#: The IAM actions the identity that starts and reconciles runs needs, per runtime (what
#: the MWAA execution role or the CI role running ``fluid apply`` must be granted). A
#: runtime in :data:`PASS_ROLE` also needs ``iam:PassRole`` on the job's execution role:
#: StartJobRun passes it (EMR Serverless: ``iam:PassedToService``
#: ``emr-serverless.amazonaws.com``; EMR on EKS: AWS's job-submitter example). Starting
#: with tags needs the service's ``TagResource`` too. Sources: the Service Authorization
#: Reference pages for EMR Serverless, EMR on EKS, Glue and EMR.
CALLER_ACTIONS: Dict[str, tuple] = {
    "emr-serverless": (
        "emr-serverless:StartJobRun",
        "emr-serverless:GetJobRun",
        "emr-serverless:ListJobRuns",
        "emr-serverless:CancelJobRun",
        "emr-serverless:TagResource",
        "emr-serverless:ListApplications",  # only without parameters.spark.applicationId
    ),
    "glue": ("glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"),
    "emr-ec2": (
        "elasticmapreduce:AddJobFlowSteps",
        "elasticmapreduce:ListSteps",
        "elasticmapreduce:DescribeStep",
        "elasticmapreduce:CancelSteps",
        # The cluster's state (a stream does not restart on an ended cluster), its Spot
        # posture, warned at plan and apply (_posture.py), and the role its steps run as,
        # checked before a step starts (EmrEc2.start).
        "elasticmapreduce:DescribeCluster",
        "elasticmapreduce:ListInstanceFleets",
        "elasticmapreduce:ListInstanceGroups",
    ),
    "emr-eks": (
        "emr-containers:StartJobRun",
        "emr-containers:DescribeJobRun",
        "emr-containers:ListJobRuns",
        "emr-containers:CancelJobRun",
        "emr-containers:TagResource",
    ),
}
PASS_ROLE = frozenset({"emr-serverless", "emr-eks"})
#: The runtimes that retry a failed run themselves, with the setting ``execution.retries``
#: becomes there: a run that ended FAILED on one has spent those retries, and forge does not
#: start it again on top of them. EMR on EC2, Glue (its job is emitted with MaxRetries 0)
#: and local runs have none: forge's reconcile restarts them
#: (:attr:`DesiredRun.restarts_per_hour`).
SERVICE_RETRIES: Dict[str, str] = {
    "emr-serverless": "EMR Serverless retries a failed stream (retryPolicy.maxFailedAttemptsPerHour)",
    "emr-eks": "EMR on EKS retries a failed driver (retryPolicyConfiguration.maxAttempts)",
}
#: What starting an EMR on EC2 step also reads: the cluster's instance-profile role and the
#: policies attached to it (:meth:`EmrEc2.start`).
EC2_ROLE_READS = ("iam:GetInstanceProfile", "iam:ListAttachedRolePolicies")

#: ``(service, region) -> boto3 client``
ClientFactory = Callable[[str, str], Any]
#: A stop key as the runner takes it (``forge_spark_runner._STOP_KEY``; a test checks they
#: agree): it names a file, so never a ``/`` or a dot.
STOP_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class SparkRunError(RuntimeError):
    """A run could not be found, started, read or stopped. The message names the run."""


class MarkerReadDenied(SparkRunError):
    """The failed-probation marker is refused, so forge cannot tell whether a config failed.

    Either the job config under the same prefix is refused too (``s3:GetObject`` is missing
    there), or only the marker is (``job_config_readable``): without ``s3:ListBucket`` a missing
    marker answers the same way, and the two cannot be told apart. ``uri`` is the marker's.
    """

    def __init__(self, message: str, *, uri: str = "", job_config_readable: bool = False) -> None:
        super().__init__(message)
        self.uri = uri
        self.job_config_readable = job_config_readable


_WARNED_PREFIXES: set = set()


def warn_no_list_bucket(uri: str) -> None:
    """Say once per prefix that a missing marker answered AccessDenied: granting
    ``s3:ListBucket`` on the build's prefix makes a missing object read as missing."""
    import logging

    prefix = uri.rsplit("/releases/", 1)[0]
    if prefix in _WARNED_PREFIXES:
        return
    _WARNED_PREFIXES.add(prefix)
    logging.getLogger(__name__).warning(
        "spark_marker_read_denied",
        extra={
            "uri": uri,
            "hint": f"a missing object reads as AccessDenied without s3:ListBucket on {prefix}/: "
            "grant it so forge can tell a missing failed-release marker from a refused read",
        },
    )


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    state: str
    raw_state: str
    config_hash: Optional[str] = None
    source_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    attempt: Optional[int] = None
    details: str = ""
    created: float = 0.0
    #: The run's processes stopped without recording how it ended, or a signal from outside
    #: forge stopped them (a local run whose machine shut down, or that was killed).
    lost: bool = False
    #: The run this one was started again after (``--fluid-restart-of``): a restart, which
    #: counts against the stream's budget.
    restart_of: Optional[str] = None
    #: The key a stream's run was started with (``--fluid-stop-key``): ``fluid apply`` asks it
    #: to stop between batches through the request it names (:func:`stop_request_uri`). None
    #: for a run an older ``fluid apply`` started: it can only be cancelled.
    stop_key: Optional[str] = None
    #: The job config the run was started on (``--config``): it looks for its stop request
    #: beside it (:func:`stop_request_uri`).
    config_uri: Optional[str] = None
    #: The digest of the job config forge started the run on (``--fluid-job-sha256``), as the
    #: runtime recorded it: what a release record of that config must hash to.
    job_sha256: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "runId": self.run_id,
            "state": self.state,
            "rawState": self.raw_state,
            "configHash": self.config_hash,
            "attempt": self.attempt,
            "details": self.details,
        }


def default_clients(service: str, region: str, *, config: Any = None) -> Any:
    """A boto3 client, from the default credential chain (``AWS_ENDPOINT_URL`` included);
    ``config``, a ``botocore.config.Config``, bounds its calls (the posture check's). The client
    is :func:`fluid_build.util.s3.default_clients`'; a missing boto3 is a :class:`SparkRunError`."""
    try:
        return _s3.default_clients(service, region, config=config)
    except _s3.S3Unavailable as exc:  # pragma: no cover - the [aws] extra is missing
        raise SparkRunError(
            "Spark job runs need boto3: pip install 'data-product-forge[aws]'"
        ) from exc


def _ts(value: Any) -> float:
    if isinstance(value, _dt.datetime):
        return value.timestamp()
    return float(value or 0)


def _identity(args: Sequence[Any], tags: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """The ``--fluid-*`` flags of a run's arguments, and its ``fluid_*`` tags (which win)."""
    found: Dict[str, str] = {}
    items = [str(a) for a in args or []]
    for flag, key in (
        ("--fluid-config-hash", "config_hash"),
        ("--fluid-source", "source_id"),
        ("--fluid-checkpoint", "checkpoint_id"),
        (RESTART_FLAG, "restart_of"),
        (STOP_KEY_FLAG, "stop_key"),
        ("--config", "config_uri"),
        ("--fluid-job-sha256", "job_sha256"),
    ):
        if flag in items[:-1]:
            found[key] = items[items.index(flag) + 1]
    for tag, key in (
        ("fluid_config_hash", "config_hash"),
        ("fluid_source", "source_id"),
        ("fluid_checkpoint", "checkpoint_id"),
    ):
        if tags and tags.get(tag):
            found[key] = str(tags[tag])
    return found


def usable_stop_key(key: Optional[str]) -> bool:
    """Whether ``key`` is a stop key the runner takes, so a request may be named by it."""
    return bool(key) and STOP_KEY.fullmatch(str(key)) is not None


def stop_request_uri(job_uri: str, key: str) -> str:
    """Where the run started with stop key ``key`` looks for its stop request: ``stop/<key>``
    beside the job config it reads (``forge_spark_runner.stop_request_uri``)."""
    return f"{job_uri.rsplit('/', 1)[0]}/stop/{key}"


#: A release record's job config, under the build's job config prefix (:func:`release_config_uri`).
_RELEASE_CONFIG = re.compile(r"releases/[0-9a-f]{16}/job\.json")
#: A job config digest as the runner's ``--fluid-job-sha256`` holds it.
_SHA256 = re.compile(r"[0-9a-f]{64}")


def release_config_uri(job_uri: str, job_sha256: str) -> str:
    """Where a release record keeps the job config whose digest is ``job_sha256``, beside the
    build's job config (``releases/<digest16>/job.json``): a stream rolled back to it reads it."""
    return f"{job_uri.rsplit('/', 1)[0]}/releases/{job_sha256[:16]}/job.json"


#: A config hash as forge records it on a run (``--fluid-config-hash``): lower-case hex.
_CONFIG_HASH = re.compile(r"[0-9a-f]{16,64}")


def failed_release_uri(job_uri: str, config_hash: str) -> str:
    """Where forge marks a config that failed its probation (``releases/<hash16>/failed``),
    beside the build's job config: a later ``fluid apply`` will not start that config again
    unless the operator says so."""
    return f"{job_uri.rsplit('/', 1)[0]}/releases/{config_hash[:16]}/failed"


def failed_release_body(config_hash: str, run_id: str, reason: str) -> bytes:
    """What a failed-probation marker holds, for whoever reads it."""
    import json
    import time

    return json.dumps(
        {
            "configHash": config_hash,
            "runId": run_id,
            "reason": reason[:400],
            "failedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        sort_keys=True,
    ).encode("utf-8")


def stop_location(info: RunInfo, job_uri: str) -> str:
    """The job config ``info`` looks for its stop request beside: ``job_uri`` (the build's),
    or a release record of the build a rollback started it on. A run whose arguments do not
    say is taken to read ``job_uri``.

    Raises:
        SparkRunError: the run was started on another job config (the output's location
            moved since): a request beside ``job_uri`` would never be seen.
    """
    config = info.config_uri
    if not config or config == job_uri:
        return job_uri
    prefix = job_uri.rsplit("/", 1)[0] + "/"
    if config.startswith(prefix) and _RELEASE_CONFIG.fullmatch(config[len(prefix) :]):
        return config
    raise SparkRunError(
        f"it was started on the job config {config}, not {job_uri} (the output's location "
        "moved), and looks for a stop request there"
    )


def _left_behind(uri: str, exc: BaseException) -> None:
    """Log a stop request that could not be removed (it stops no other run)."""
    import logging

    logging.getLogger(__name__).warning(
        "spark_stop_request_left",
        extra={"uri": uri, "error": _error_code(exc) or type(exc).__name__},
    )


def stop_request_body(run: DesiredRun) -> bytes:
    """What a stop request holds, for whoever reads it: the runner only looks for the file."""
    import json
    import time

    return json.dumps(
        {
            "stop": "between batches",
            "requestedBy": "fluid apply",
            "configHash": run.config_hash,
            "requestedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        sort_keys=True,
    ).encode("utf-8")


def _conf_params(conf: Mapping[str, str]) -> List[str]:
    out: List[str] = []
    for name in sorted(conf):
        out += ["--conf", f"{name}={conf[name]}"]
    return out


def _submit_parameters(conf: Mapping[str, str]) -> str:
    """``sparkSubmitParameters`` as one string; a value holding a space is quoted."""
    import shlex

    return " ".join(shlex.quote(p) for p in _conf_params(conf))


#: The most bytes an artifact read back before a start may hold (forge's are tens of KiB).
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


def checked_run(r: DesiredRun, clients: ClientFactory) -> DesiredRun:
    """The run, with the digest of the job config its runtime reads
    (``--fluid-job-sha256``), once the config and the runner read back from S3 are the
    ones this apply compiled: the runner then refuses any other config, so one another
    apply put there since never runs under this run's config hash. The artifact
    bucket's write access stays the boundary: a writer could replace the runner after
    this read.

    Raises:
        SparkRunError: an artifact cannot be read, or is another one; nothing started.
    """
    return _checked(r, clients)[0]


def _checked(r: DesiredRun, clients: ClientFactory) -> "tuple[DesiredRun, bytes]":
    """:func:`checked_run`, and the job config's bytes as the runtime reads them (with the MSK
    brokers OpenTofu fills in), which a release record keeps."""
    import dataclasses
    import hashlib
    import unicodedata

    from fluid_build.build_runners.spark._desired import config_digest
    from fluid_build.spark import runner_source

    config, runner = _artifact(r, clients, r.job_uri), _artifact(r, clients, r.runner_uri)
    # OpenTofu writes text in NFC; the runner's is the one this forge-cli ships.
    shipped = unicodedata.normalize("NFC", runner_source()).encode("utf-8")
    if (
        not r.config_matches(config)
        or hashlib.sha256(runner).digest() != hashlib.sha256(shipped).digest()
    ):
        raise SparkRunError(
            f"Spark build {r.build}: {r.job_uri} or its runner is not the one this apply "
            "compiled (another apply has changed it since); nothing was started. Run fluid "
            "apply again: it uploads them and starts the run on them"
        )
    return dataclasses.replace(r, job_sha256=config_digest(r.job_text)), config


def _read_back(clients: ClientFactory, region: str, uri: str) -> bytes:
    """An object's bytes from S3, at most one more than :data:`MAX_ARTIFACT_BYTES` (more
    is no artifact forge wrote). Raises what boto3 raises."""
    return _s3.read_back(clients, region, uri, MAX_ARTIFACT_BYTES + 1)


def _artifact(run: DesiredRun, clients: ClientFactory, uri: str) -> bytes:
    """An artifact's bytes from S3, at most :data:`MAX_ARTIFACT_BYTES`."""
    try:
        data = _read_back(clients, run.region, uri)
    except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
        code = _error_code(exc)
        raise SparkRunError(
            f"Spark build {run.build}: {uri} cannot be read to check it before the run "
            f"starts ({code or type(exc).__name__}); nothing was started"
        ) from exc
    if len(data) > MAX_ARTIFACT_BYTES:
        raise SparkRunError(
            f"Spark build {run.build}: {uri} is larger than any artifact forge writes; "
            "nothing was started"
        )
    return data


class Adapter:
    runtime = ""
    service = ""
    #: ``fluid apply`` uploads the run's job config ahead of it (OpenTofu, on AWS), which
    #: ``--mode reconcile`` reads back (:meth:`deployed_job`).
    uploads_job = True

    def __init__(self, run: DesiredRun, clients: Optional[ClientFactory]) -> None:
        self.run = run
        self.clients = clients
        # A local run has no AWS client: its "client" is the machine (``Local.host``).
        self.client = clients(self.service, run.region) if clients is not None else None
        self._checked: Optional[DesiredRun] = None
        #: The job config's bytes :meth:`checked` read back, which :meth:`keep_release` keeps.
        self._checked_body: Optional[bytes] = None

    def checked(self) -> DesiredRun:
        """:func:`checked_run` of this adapter's run, read once: a reconcile checks before it
        cancels the run of another config (a refusal leaves that run going), and the start
        then uses the run it checked."""
        if self._checked is None:
            self._checked, self._checked_body = _checked(self.run, self.clients)
        return self._checked

    def runs(self, since: Optional[float] = None) -> List[RunInfo]:
        """The build's runs, newest first: every active one and the most recent ended ones,
        reaching back to ``since`` (epoch seconds) where the runtime lists by page."""
        raise NotImplementedError

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        """Start the run; ``restart_of`` names the run that died, which it is started again
        after (:meth:`DesiredRun.entry_args`)."""
        raise NotImplementedError

    def check_target(self) -> None:
        """Raise :class:`SparkRunError` when the run's target can take no new run (an EMR
        cluster that ended); nothing where the runtime has no such state."""

    def deployed_job(self) -> Optional[bytes]:
        """The job config the last ``fluid apply`` uploaded (``job.json`` at the run's job
        URI), or None when there is none.

        Raises:
            SparkRunError: it cannot be read (the message names the object and the code).
        """
        uri = self.run.job_uri
        try:
            body = _read_back(self.clients or default_clients, self.run.region, uri)
        except Exception as exc:  # noqa: BLE001 - botocore is the [aws] extra
            code = _error_code(exc) or type(exc).__name__
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            denied = (
                " (without s3:ListBucket on the bucket, a missing object reads as AccessDenied "
                "too, and with SSE-KMS the identity also needs kms:Decrypt on its key)"
                if code in ("AccessDenied", "403", "Forbidden")
                else ""
            )
            raise SparkRunError(
                f"the job config the last fluid apply uploaded ({uri}) cannot be read: "
                f"{code}; the identity that reconciles needs s3:GetObject on it{denied}"
            ) from exc
        if len(body) > MAX_ARTIFACT_BYTES:
            raise SparkRunError(
                f"the job config at {uri} is over {MAX_ARTIFACT_BYTES} bytes, more than forge "
                "ever uploads, so it is not the one fluid apply wrote; run fluid apply"
            )
        return body

    def get(self, run_id: str) -> RunInfo:
        raise NotImplementedError

    def cancel(self, run_id: str) -> None:
        raise NotImplementedError

    def request_stop(self, info: RunInfo) -> Optional[str]:
        """Ask ``info``, a stream's run of this build, to stop between batches: the object
        :func:`stop_request_uri` names beside this build's job config, in the bucket
        ``fluid apply`` uploads the job config to. Its URI, or None when the run cannot be
        asked (no stop key: an older ``fluid apply`` started it).

        Raises:
            SparkRunError: the request could not be written, or the run looks for one beside
                another job config (:func:`stop_location`; the message names it).
        """
        uri = self.stop_request_for(info)
        if uri is not None:
            self.put_stop_request(uri)
        return uri

    def stop_request_for(self, info: RunInfo) -> Optional[str]:
        """Where :meth:`request_stop` writes ``info``'s request, without writing it: None when
        the run cannot be asked (no stop key).

        Raises:
            SparkRunError: the run looks for one beside another job config.
        """
        if not usable_stop_key(info.stop_key):
            return None
        return stop_request_uri(stop_location(info, self.run.job_uri), str(info.stop_key))

    def put_stop_request(self, uri: str) -> None:
        """Write the stop request at ``uri`` (:meth:`stop_request_for`).

        Raises:
            SparkRunError: the request could not be written (the message names it).
        """
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            (self.clients or default_clients)("s3", self.run.region).put_object(
                Bucket=bucket,
                Key=key,
                Body=stop_request_body(self.run),
                ContentType="application/json",
            )
        except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
            code = _error_code(exc) or type(exc).__name__
            raise SparkRunError(f"the stop request {uri} could not be written: {code}") from exc

    def sibling(self, run: DesiredRun) -> "Adapter":
        """An adapter for ``run``, another run of this build (a rollback's), with this one's
        clients."""
        return type(self)(run, self.clients)

    def keep_release(self, run: DesiredRun) -> str:
        """Keep the job config ``run`` was started on as a release record
        (:func:`release_config_uri`): the bytes :meth:`checked` read back, which a later
        release rolls back to. Its URI.

        Raises:
            SparkRunError: the record could not be written (the message names it).
        """
        from fluid_build.build_runners.spark._desired import config_digest

        body = self._checked_body
        if body is None:
            body = _artifact(run, self.clients or default_clients, run.job_uri)
        uri = release_config_uri(run.job_uri, run.job_sha256 or config_digest(run.job_text))
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            (self.clients or default_clients)("s3", run.region).put_object(
                Bucket=bucket, Key=key, Body=body, ContentType="application/json"
            )
        except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
            code = _error_code(exc) or type(exc).__name__
            raise SparkRunError(f"{uri} could not be written: {code}") from exc
        return uri

    def release_config(self, info: RunInfo) -> Optional[bytes]:
        """The job config the release record of ``info``'s digest holds, or None when there is
        none (or ``info`` carries no digest). The caller checks the bytes against the digest.

        Raises:
            SparkRunError: the record cannot be read (the message names it and the code).
        """
        if not info.job_sha256 or not _SHA256.fullmatch(info.job_sha256):
            return None
        uri = release_config_uri(self.run.job_uri, info.job_sha256)
        try:
            return _read_back(self.clients or default_clients, self.run.region, uri)
        except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
            code = _error_code(exc) or type(exc).__name__
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise SparkRunError(f"the release record {uri} cannot be read: {code}") from exc

    def withdraw_stop(self, uri: str) -> None:
        """Delete a stop request once its run has ended or been cancelled. Best effort: one
        left behind stops no other run, since its key is that run's own."""
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            (self.clients or default_clients)("s3", self.run.region).delete_object(
                Bucket=bucket, Key=key
            )
        except Exception as exc:  # noqa: BLE001 - best effort, as the docstring says
            _left_behind(uri, exc)

    def mark_failed(self, config_hash: str, run_id: str, reason: str) -> str:
        """Mark the config with ``config_hash`` as one that failed its probation
        (:func:`failed_release_uri`). Its URI.

        Raises:
            SparkRunError: the marker could not be written (the message names it).
        """
        uri = failed_release_uri(self.run.job_uri, config_hash)
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            (self.clients or default_clients)("s3", self.run.region).put_object(
                Bucket=bucket,
                Key=key,
                Body=failed_release_body(config_hash, run_id, reason),
                ContentType="application/json",
            )
        except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
            code = _error_code(exc) or type(exc).__name__
            raise SparkRunError(f"{uri} could not be written: {code}") from exc
        return uri

    def failed_release(self, config_hash: Optional[str]) -> Optional[bytes]:
        """The marker of a config that failed its probation, or None when there is none (or
        ``config_hash`` is no hash forge records).

        Raises:
            SparkRunError: the marker cannot be read (the message names it and the code).
        """
        if not config_hash or not _CONFIG_HASH.fullmatch(config_hash):
            return None
        uri = failed_release_uri(self.run.job_uri, config_hash)
        try:
            return _read_back(self.clients or default_clients, self.run.region, uri)
        except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
            code = _error_code(exc) or type(exc).__name__
            if is_missing_code(code):
                return None
            if is_denied_code(code):
                # Without s3:ListBucket a missing key answers 403, so a refused marker and a
                # missing one look alike. The job config under the same prefix (which must
                # exist) tells the two cases apart, and neither is read as "no marker".
                try:
                    _read_back(self.clients or default_clients, self.run.region, self.run.job_uri)
                except Exception as probe:  # noqa: BLE001 - botocore's errors
                    probe_code = _error_code(probe) or type(probe).__name__
                    if not (is_denied_code(probe_code) or is_missing_code(probe_code)):
                        raise SparkRunError(
                            f"the marker {uri} is refused ({code}) and the job config "
                            f"{self.run.job_uri} could not be read to tell why ({probe_code}): "
                            "nothing was checked"
                        ) from probe
                    raise MarkerReadDenied(
                        f"the marker {uri} and the job config {self.run.job_uri} cannot be read "
                        f"({probe_code}): the identity that reconciles needs s3:GetObject on the "
                        "build's prefix, and s3:ListBucket on it so a missing object reads as "
                        "missing",
                        uri=uri,
                    ) from probe
                raise MarkerReadDenied(
                    f"the marker {uri} is refused ({code}) while the job config beside it reads: "
                    "the identity lacks s3:GetObject on it, or s3:ListBucket on the build's "
                    "prefix, which makes a missing marker answer the same way",
                    uri=uri,
                    job_config_readable=True,
                ) from exc
            raise SparkRunError(f"the marker {uri} cannot be read: {code}") from exc

    def clear_failed(self, config_hash: str) -> None:
        """Remove a failed-probation marker once the operator has started its config again.
        Best effort: a marker left behind only asks for the same go-ahead again."""
        if not _CONFIG_HASH.fullmatch(config_hash):
            return
        uri = failed_release_uri(self.run.job_uri, config_hash)
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            (self.clients or default_clients)("s3", self.run.region).delete_object(
                Bucket=bucket, Key=key
            )
        except Exception as exc:  # noqa: BLE001 - best effort, as the docstring says
            _left_behind(uri, exc)

    def where(self) -> str:
        return ", ".join(f"{k} {v}" for k, v in self.run.target.items())


# --- EMR Serverless ------------------------------------------------------------------

_SERVERLESS = {
    "SUBMITTED": PENDING,
    "PENDING": PENDING,
    "SCHEDULED": PENDING,
    "QUEUED": PENDING,
    "RUNNING": RUNNING,
    "SUCCESS": SUCCEEDED,
    "FAILED": FAILED,
    "CANCELLING": CANCELLING,
    "CANCELLED": CANCELLED,
}


def _required_id(record: Mapping[str, Any], what: str, key: str = "id") -> str:
    """An EMR Serverless record's id field (AWS always returns it: ``id`` on list summaries,
    ``jobRunId`` on a job run).

    Raises:
        SparkRunError: a record without it, naming the keys it has (never their values).
    """
    ident = record.get(key)
    if not ident:
        raise SparkRunError(
            f"an EMR Serverless {what} carries no {key}; its keys: "
            f"{', '.join(sorted(map(str, record))) or 'none'}"
        )
    return str(ident)


_SERVERLESS_ACTIVE = frozenset(
    {"SUBMITTED", "PENDING", "SCHEDULED", "QUEUED", "RUNNING", "CANCELLING"}
)


class EmrServerless(Adapter):
    runtime, service = "emr-serverless", "emr-serverless"
    #: How far back the reconcile looks for this build's last run.
    LOOKBACK_PAGES = 4

    def __init__(self, run: DesiredRun, clients: ClientFactory) -> None:
        super().__init__(run, clients)
        self._app: Optional[str] = run.target.get("applicationId")
        start = self.client.meta.service_model.operation_model("StartJobRun").input_shape
        if "mode" not in start.members or "retryPolicy" not in start.members:
            raise SparkRunError(
                "this boto3 predates EMR Serverless streaming job runs (StartJobRun mode and "
                "retryPolicy); upgrade it: pip install -U boto3"
            )

    def application_id(self) -> str:
        if self._app:
            return self._app
        name = self.run.target["applicationName"]
        found = []
        for page in self.client.get_paginator("list_applications").paginate():
            found += [
                _required_id(a, "application summary")
                for a in page.get("applications") or []
                if a.get("name") == name and a.get("state") not in ("TERMINATING", "TERMINATED")
            ]
        if len(found) != 1:
            raise SparkRunError(
                f"Spark build {self.run.build}: "
                + (
                    f"no EMR Serverless application named {name} in {self.run.region}; "
                    "fluid apply creates it, or set parameters.spark.applicationId"
                    if not found
                    else f"{len(found)} EMR Serverless applications are named {name}; set "
                    "parameters.spark.applicationId"
                )
            )
        self._app = found[0]
        return self._app

    def where(self) -> str:
        return f"EMR Serverless application {self.application_id()}"

    def _info(self, job: Mapping[str, Any]) -> RunInfo:
        driver = (job.get("jobDriver") or {}).get("sparkSubmit") or {}
        ident = _identity(driver.get("entryPointArguments") or [], job.get("tags"))
        raw = str(job.get("state") or "")
        return RunInfo(
            run_id=_required_id(job, "job run", "jobRunId"),
            state=_SERVERLESS.get(raw, PENDING),
            raw_state=raw,
            attempt=job.get("attempt"),
            details=str(job.get("stateDetails") or ""),
            created=_ts(job.get("createdAt")),
            **ident,
        )

    def runs(self, since: Optional[float] = None) -> List[RunInfo]:
        """Every active run of this build (all pages, filtered by state: a long-running job
        in a shared application is older than its newest runs), then its most recent ended
        ones from :data:`LOOKBACK_PAGES` pages, each one back to ``since`` among them. Both
        modes: a stream moved to availableNow still finds its old continuous job."""
        app = self.application_id()
        paginator = self.client.get_paginator("list_job_runs")
        found: Dict[str, Mapping[str, Any]] = {}
        for page in paginator.paginate(applicationId=app, states=sorted(_SERVERLESS_ACTIVE)):
            found.update(
                {
                    _required_id(j, "job run summary"): j
                    for j in page.get("jobRuns") or []
                    if self._mine(j)
                }
            )
        active = set(found)
        for number, page in enumerate(paginator.paginate(applicationId=app)):
            for j in page.get("jobRuns") or []:
                if self._mine(j):
                    found.setdefault(_required_id(j, "job run summary"), j)
            if number + 1 >= self.LOOKBACK_PAGES:
                break
        ordered = sorted(found.items(), key=lambda kv: _ts(kv[1].get("createdAt")), reverse=True)
        ended = [rid for rid, _ in ordered if rid not in active]
        # The last few, and every one back to ``since``: a restart budget counts them all.
        kept = set(ended[:5]) | {
            rid for rid in ended if since is not None and _ts(found[rid].get("createdAt")) >= since
        }
        return [self.get(rid) for rid, _ in ordered if rid in active or rid in kept]

    def _mine(self, summary: Mapping[str, Any]) -> bool:
        return summary.get("name") == self.run.run_name

    def get(self, run_id: str) -> RunInfo:
        job = self.client.get_job_run(applicationId=self.application_id(), jobRunId=run_id)
        return self._info(job["jobRun"])

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        r = self.checked()
        request: Dict[str, Any] = {
            "applicationId": self.application_id(),
            "clientToken": token,
            "executionRoleArn": r.role_arn,
            "name": r.run_name,
            "mode": "BATCH" if r.scheduled else "STREAMING",
            "jobDriver": {
                "sparkSubmit": {
                    "entryPoint": r.runner_uri,
                    "entryPointArguments": r.entry_args(restart_of, stop_key=token),
                    "sparkSubmitParameters": _submit_parameters(r.spark_conf),
                }
            },
            "tags": r.tags(),
        }
        if r.retry_attempts:
            key = "maxAttempts" if r.scheduled else "maxFailedAttemptsPerHour"
            request["retryPolicy"] = {key: r.retry_attempts}
        return str(self.client.start_job_run(**request)["jobRunId"])

    def cancel(self, run_id: str) -> None:
        self.client.cancel_job_run(applicationId=self.application_id(), jobRunId=run_id)

    def application_runs(self) -> List[RunInfo]:
        """Every active run in the application, whichever build started it: for an
        application a destroy deletes, which can stop only once they have all ended."""
        paginator = self.client.get_paginator("list_job_runs")
        ids: List[str] = []
        for page in paginator.paginate(
            applicationId=self.application_id(), states=sorted(_SERVERLESS_ACTIVE)
        ):
            ids += [_required_id(j, "job run summary") for j in page.get("jobRuns") or []]
        return [self.get(run_id) for run_id in ids]

    def application_state(self) -> str:
        found = self.client.get_application(applicationId=self.application_id())
        return str((found.get("application") or {}).get("state") or "")

    def stop_application(self) -> None:
        self.client.stop_application(applicationId=self.application_id())


# --- AWS Glue ------------------------------------------------------------------------

_GLUE = {
    "STARTING": PENDING,
    "WAITING": PENDING,
    "RUNNING": RUNNING,
    "STOPPING": CANCELLING,
    "STOPPED": CANCELLED,
    "SUCCEEDED": SUCCEEDED,
    "FAILED": FAILED,
    "TIMEOUT": FAILED,
    "ERROR": FAILED,
    "EXPIRED": FAILED,
}


class Glue(Adapter):
    runtime, service = "glue", "glue"

    @property
    def job_name(self) -> str:
        return self.run.target["glueJobName"]

    def where(self) -> str:
        return f"Glue job {self.job_name}"

    def _info(self, job: Mapping[str, Any]) -> RunInfo:
        arguments = job.get("Arguments") or {}
        flat: List[str] = []
        for key, value in arguments.items():
            flat += [str(key), str(value)]
        raw = str(job.get("JobRunState") or "")
        return RunInfo(
            run_id=str(job.get("Id")),
            state=_GLUE.get(raw, PENDING),
            raw_state=raw,
            attempt=job.get("Attempt"),
            details=str(job.get("ErrorMessage") or job.get("StateDetail") or ""),
            created=_ts(job.get("StartedOn")),
            **_identity(flat),
        )

    def runs(self, since: Optional[float] = None) -> List[RunInfo]:
        page = self.client.get_job_runs(JobName=self.job_name, MaxResults=25)
        runs = [self._info(j) for j in page.get("JobRuns") or []]
        return sorted(runs, key=lambda i: i.created, reverse=True)

    def get(self, run_id: str) -> RunInfo:
        return self._info(self.client.get_job_run(JobName=self.job_name, RunId=run_id)["JobRun"])

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        r = self.checked()
        args = r.entry_args(restart_of, stop_key=token)
        arguments = {args[i]: args[i + 1] for i in range(0, len(args), 2)}
        if r.spark_conf:
            # Glue takes Spark settings as one --conf argument: "a=b --conf c=d".
            pairs = [f"{k}={r.spark_conf[k]}" for k in sorted(r.spark_conf)]
            arguments["--conf"] = " --conf ".join(pairs)
        # The Glue job is emitted with MaxRetries 0: forge's reconcile restarts a stream.
        out = self.client.start_job_run(JobName=self.job_name, Arguments=arguments)
        return str(out["JobRunId"])

    def cancel(self, run_id: str) -> None:
        out = self.client.batch_stop_job_run(JobName=self.job_name, JobRunIds=[run_id])
        for error in out.get("Errors") or []:
            detail = (error.get("ErrorDetail") or {}).get("ErrorMessage") or ""
            raise SparkRunError(f"Glue run {run_id} of {self.job_name} was not stopped: {detail}")


# --- EMR on EC2 ----------------------------------------------------------------------

#: The cluster states AddJobFlowSteps takes a step in.
_LIVE_CLUSTER = frozenset({"STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"})


def _created(step: Mapping[str, Any]) -> float:
    return _ts(((step.get("Status") or {}).get("Timeline") or {}).get("CreationDateTime"))


_STEP = {
    "PENDING": PENDING,
    "RUNNING": RUNNING,
    "CANCEL_PENDING": CANCELLING,
    "COMPLETED": SUCCEEDED,
    "CANCELLED": CANCELLED,
    "FAILED": FAILED,
    "INTERRUPTED": FAILED,
}


class EmrEc2(Adapter):
    runtime, service = "emr-ec2", "emr"
    LOOKBACK_PAGES = 10

    @property
    def cluster(self) -> str:
        return self.run.target["clusterId"]

    def where(self) -> str:
        return f"EMR cluster {self.cluster}"

    def _info(self, step: Mapping[str, Any]) -> RunInfo:
        status = step.get("Status") or {}
        raw = str(status.get("State") or "")
        failure = status.get("FailureDetails") or {}
        reason = status.get("StateChangeReason") or {}
        return RunInfo(
            run_id=str(step.get("Id")),
            state=_STEP.get(raw, PENDING),
            raw_state=raw,
            details=str(failure.get("Message") or reason.get("Message") or ""),
            created=_created(step),
            **_identity((step.get("Config") or {}).get("Args") or []),
        )

    def runs(self, since: Optional[float] = None) -> List[RunInfo]:
        """Every active step of this build (all pages: a shared cluster's newer steps can push
        a long-running one off the first), then its ended ones back to its last one, and back
        to ``since`` (ListSteps lists newest first), at most :data:`LOOKBACK_PAGES` pages."""
        steps: Dict[str, Mapping[str, Any]] = {}
        pages = self.client.get_paginator("list_steps").paginate(
            ClusterId=self.cluster, StepStates=["PENDING", "RUNNING", "CANCEL_PENDING"]
        )
        for page in pages:
            steps.update({str(s.get("Id")): s for s in page.get("Steps") or []})
        active = set(steps)
        going = any(s.get("Name") == self.run.run_name for s in steps.values())
        # Ended steps, newest first, until this build's last one (a busy shared cluster's
        # newer steps would otherwise hide a failed run, and every apply would restart it).
        recent = self.client.get_paginator("list_steps").paginate(ClusterId=self.cluster)
        found_ended = False
        for number, page in enumerate(recent):
            page_steps = page.get("Steps") or []
            for step in page_steps:
                steps.setdefault(str(step.get("Id")), step)
            found_ended = found_ended or any(
                s.get("Name") == self.run.run_name and str(s.get("Id")) not in active
                for s in page_steps
            )
            # A restart budget counts the build's steps of the last hour: the page that
            # reaches back past ``since`` holds the oldest of them.
            reached = since is None or any(_created(s) < since for s in page_steps)
            # A build with a step going needs no older end than the window.
            if number + 1 >= self.LOOKBACK_PAGES or ((found_ended or going) and reached):
                break
        mine = [s for s in steps.values() if s.get("Name") == self.run.run_name]
        return sorted((self._info(s) for s in mine), key=lambda i: i.created, reverse=True)

    def get(self, run_id: str) -> RunInfo:
        return self._info(self.client.describe_step(ClusterId=self.cluster, StepId=run_id)["Step"])

    #: The cluster as DescribeCluster returned it for the next start (``(None,)`` when it
    #: could not be read): the ended-cluster check and the role check share one read.
    _cluster_read: Optional[tuple] = None

    def _described(self) -> Optional[Mapping[str, Any]]:
        """The cluster, read once for the next start; None when this identity cannot."""
        if self._cluster_read is None:
            try:
                found = self.client.describe_cluster(ClusterId=self.cluster)["Cluster"]
            except Exception:  # noqa: BLE001 - unreadable: the start reports what EMR says
                found = None
            self._cluster_read = (found,)
        cluster: Optional[Mapping[str, Any]] = self._cluster_read[0]
        return cluster

    def check_target(self) -> None:
        """Refuse a cluster that takes no new step: AddJobFlowSteps accepts only a cluster
        that is STARTING, BOOTSTRAPPING, RUNNING or WAITING, so a stream whose cluster ended
        (terminated, or with errors: its primary node lost) cannot start there again. A
        cluster this identity cannot read is left to AddJobFlowSteps to report."""
        cluster = self._described()
        if cluster is None:
            return
        status = cluster.get("Status") or {}
        state = str(status.get("State") or "")
        if not state or state in _LIVE_CLUSTER:
            return
        reason = status.get("StateChangeReason") or {}
        why = ": ".join(str(p) for p in (reason.get("Code"), reason.get("Message")) if p)
        raise SparkRunError(
            f"EMR cluster {self.cluster} is {state}{f' ({why})' if why else ''} and takes no "
            "new step, so the stream cannot start there again. Point "
            "parameters.spark.clusterId at a running cluster and run fluid apply: the stream "
            "resumes from its checkpoint"
        )

    def _role_policies(self) -> Optional[Dict[str, List[str]]]:
        """The roles the cluster's instance profile holds, each with the names of the
        policies attached to it; None when this identity cannot read them."""
        cluster = self._described()
        if cluster is None:
            return None
        try:
            profile = str((cluster.get("Ec2InstanceAttributes") or {}).get("IamInstanceProfile"))
            iam = self.clients("iam", self.run.region)
            found = iam.get_instance_profile(InstanceProfileName=profile.rsplit("/", 1)[-1])
            roles: Dict[str, List[str]] = {}
            for role in found["InstanceProfile"].get("Roles") or []:
                pages = iam.get_paginator("list_attached_role_policies")
                roles[role["RoleName"]] = [
                    p["PolicyName"]
                    for page in pages.paginate(RoleName=role["RoleName"])
                    for p in page.get("AttachedPolicies") or []
                ]
            return roles
        except Exception:  # noqa: BLE001 - unreadable: the step starts as it always did
            return None

    def _refuse_unless_attached(self) -> None:
        """A step runs as the cluster's instance-profile role, which the cluster's owner
        holds: the build's least-privilege policy, which ``fluid apply`` creates, must be
        attached to it before a step can read and write what the build does. A step started
        without it fails at once, and each such failure would spend a stream's restart
        budget, so the start is refused until it is attached. A build that names its own
        ``executionRoleArn`` has no forge policy to check."""
        policy = _names.execution_role_name(self.run.product, self.run.build)
        if not self.run.role_arn.endswith(f":role/{policy}"):
            return
        roles = self._role_policies()
        if roles is None or any(policy in names for names in roles.values()):
            return
        role = next(iter(roles), "<the instance-profile role>")
        arn = self.run.role_arn.replace(":role/", ":policy/")
        raise SparkRunError(
            f"not started: policy {policy} is not attached to {role}, the role its steps run "
            f"as on {self.cluster}. Attach it, then apply again: aws iam attach-role-policy "
            f"--role-name {role} --policy-arn {arn}"
        )

    def _refuse_one_step_at_a_time(self) -> None:
        """A maintenance step needs a cluster that runs steps side by side: with
        ``StepConcurrencyLevel`` 1, EMR runs one step at a time, and a step that is running (a
        stream's never ends) would hold the maintenance step PENDING for as long as it runs. A
        cluster this identity cannot read is left to EMR."""
        cluster = self._described()
        level = (cluster or {}).get("StepConcurrencyLevel")
        if not isinstance(level, int) or level > 1:
            return
        running = self.client.list_steps(ClusterId=self.cluster, StepStates=["RUNNING"])
        busy = [s for s in running.get("Steps") or [] if s.get("Name") != self.run.run_name]
        if busy:
            raise SparkRunError(
                f"not started: EMR cluster {self.cluster} runs one step at a time "
                f"(StepConcurrencyLevel 1) and step {busy[0].get('Id')} is running, so the "
                "maintenance step would wait behind it for as long as it runs. Let the cluster "
                f"run steps side by side: aws emr modify-cluster --cluster-id {self.cluster} "
                "--step-concurrency-level 2"
            )

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        try:
            self._refuse_unless_attached()
            if self.run.maintains:
                self._refuse_one_step_at_a_time()
        finally:
            self._cluster_read = None  # a later start reads the cluster again
        r = self.checked()
        args = [
            "spark-submit",
            # Client mode: the driver is the step's own process, so cancelling the step
            # stops the job. In cluster mode the YARN application outlives a cancelled
            # step, and a restart would put a second writer on the checkpoint.
            "--deploy-mode",
            "client",
            *_conf_params(r.spark_conf),
            r.runner_uri,
            *r.entry_args(restart_of, stop_key=token),
        ]
        out = self.client.add_job_flow_steps(
            JobFlowId=self.cluster,
            Steps=[
                {
                    "Name": r.run_name,
                    # A failed step leaves the shared cluster running for everyone else.
                    "ActionOnFailure": "CONTINUE",
                    "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args},
                }
            ],
        )
        return str(out["StepIds"][0])

    def cancel(self, run_id: str) -> None:
        out = self.client.cancel_steps(
            ClusterId=self.cluster, StepIds=[run_id], StepCancellationOption="SEND_INTERRUPT"
        )
        for item in out.get("CancelStepsInfoList") or []:
            if item.get("Status") == "FAILED":
                raise SparkRunError(
                    f"step {run_id} on {self.cluster} was not cancelled: {item.get('Reason')}"
                )


# --- EMR on EKS ----------------------------------------------------------------------

_EKS = {
    "PENDING": PENDING,
    "SUBMITTED": PENDING,
    "RUNNING": RUNNING,
    "CANCEL_PENDING": CANCELLING,
    "COMPLETED": SUCCEEDED,
    "FAILED": FAILED,
    "CANCELLED": CANCELLED,
}


class EmrEks(Adapter):
    runtime, service = "emr-eks", "emr-containers"

    @property
    def cluster(self) -> str:
        return self.run.target["virtualClusterId"]

    def where(self) -> str:
        return f"EMR on EKS virtual cluster {self.cluster}"

    def _info(self, job: Mapping[str, Any]) -> RunInfo:
        driver = (job.get("jobDriver") or {}).get("sparkSubmitJobDriver") or {}
        raw = str(job.get("state") or "")
        execution = job.get("retryPolicyExecution") or {}
        return RunInfo(
            run_id=str(job.get("id")),
            state=_EKS.get(raw, PENDING),
            raw_state=raw,
            attempt=execution.get("currentAttemptCount"),
            details=str(job.get("stateDetails") or job.get("failureReason") or ""),
            created=_ts(job.get("createdAt")),
            **_identity(driver.get("entryPointArguments") or [], job.get("tags")),
        )

    def runs(self, since: Optional[float] = None) -> List[RunInfo]:
        page = self.client.list_job_runs(virtualClusterId=self.cluster, name=self.run.run_name)
        runs = [self._info(j) for j in page.get("jobRuns") or []]
        return sorted(runs, key=lambda i: i.created, reverse=True)

    def get(self, run_id: str) -> RunInfo:
        out = self.client.describe_job_run(id=run_id, virtualClusterId=self.cluster)
        return self._info(out["jobRun"])

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        r = self.checked()
        request: Dict[str, Any] = {
            "name": r.run_name,
            "virtualClusterId": self.cluster,
            "clientToken": token,
            "executionRoleArn": r.role_arn,
            "releaseLabel": r.release_label,
            "jobDriver": {
                "sparkSubmitJobDriver": {
                    "entryPoint": r.runner_uri,
                    "entryPointArguments": r.entry_args(restart_of, stop_key=token),
                    "sparkSubmitParameters": _submit_parameters(r.spark_conf),
                }
            },
            "tags": r.tags(),
        }
        if r.retry_attempts:
            request["retryPolicyConfiguration"] = {"maxAttempts": r.retry_attempts}
        return str(self.client.start_job_run(**request)["id"])

    def cancel(self, run_id: str) -> None:
        self.client.cancel_job_run(id=run_id, virtualClusterId=self.cluster)


# --- local -----------------------------------------------------------------------------


class Local(Adapter):
    """``runtime: local``: spark-submit on the machine ``fluid apply`` runs on.

    Its "client" is the :class:`~._local.LocalHost`: the contract's directory, which holds
    the runs' state, and the operator's environment, which names spark-submit, its
    properties file and the artifact root."""

    runtime, service = "local", ""
    uploads_job = False

    def __init__(self, run: DesiredRun, host: Any) -> None:
        from fluid_build.build_runners.spark._local import LocalRuns, default_host, require_posix

        require_posix()
        super().__init__(run, None)
        self.host = host if host is not None else default_host()
        self._runs = LocalRuns(self.host, run.product, run.run_key)

    def where(self) -> str:
        return self._runs.where()

    def runs(self, since: Optional[float] = None) -> List[RunInfo]:
        return self._runs.runs()

    def get(self, run_id: str) -> RunInfo:
        return self._runs.get(run_id)

    def checked(self) -> DesiredRun:
        """Nothing is read back: each start writes this run's artifacts itself."""
        return self.run

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        from fluid_build.build_runners.spark._local import start_run
        from fluid_build.spark import runner_source

        return start_run(
            self.host, self.run, token, runner_text=runner_source(), restart_of=restart_of
        )

    def deployed_job(self) -> Optional[bytes]:
        """None: nothing uploads a local run's job config ahead of it; each start writes the
        one the contract compiles to (:func:`~._local.publish`)."""
        return None

    def cancel(self, run_id: str) -> None:
        self._runs.cancel(run_id)

    def request_stop(self, info: RunInfo) -> Optional[str]:
        """The request beside the job config at this machine's artifact root (a file, or an
        ``s3://`` object): :meth:`Adapter.request_stop`."""
        from fluid_build.build_runners.spark._local import bound_job_uri, write_stop_request

        uri = self.stop_request_for(info)
        if uri is not None:
            self.put_stop_request(uri)
        return uri

    def stop_request_for(self, info: RunInfo) -> Optional[str]:
        from fluid_build.build_runners.spark._local import bound_job_uri

        if not usable_stop_key(info.stop_key):
            return None
        where = stop_location(info, bound_job_uri(self.host, self.run))
        return stop_request_uri(where, str(info.stop_key))

    def put_stop_request(self, uri: str) -> None:
        from fluid_build.build_runners.spark._local import put_stop_request

        put_stop_request(self.host, self.run, uri)

    def withdraw_stop(self, uri: str) -> None:
        from fluid_build.build_runners.spark._local import remove_stop_request

        remove_stop_request(self.host, uri)

    def sibling(self, run: DesiredRun) -> "Adapter":
        return Local(run, self.host)

    def keep_release(self, run: DesiredRun) -> str:
        """The record under this machine's artifact root (:meth:`Adapter.keep_release`): the
        bytes forge wrote for the run."""
        from fluid_build.build_runners.spark._local import keep_release

        return keep_release(self.host, run)

    def release_config(self, info: RunInfo) -> Optional[bytes]:
        from fluid_build.build_runners.spark._local import read_release

        if not info.job_sha256 or not _SHA256.fullmatch(info.job_sha256):
            return None
        return read_release(self.host, self.run, info.job_sha256)

    def mark_failed(self, config_hash: str, run_id: str, reason: str) -> str:
        from fluid_build.build_runners.spark._local import keep_failed

        return keep_failed(
            self.host, self.run, config_hash, failed_release_body(config_hash, run_id, reason)
        )

    def failed_release(self, config_hash: Optional[str]) -> Optional[bytes]:
        from fluid_build.build_runners.spark._local import read_failed

        if not config_hash or not _CONFIG_HASH.fullmatch(config_hash):
            return None
        return read_failed(self.host, self.run, config_hash)

    def clear_failed(self, config_hash: str) -> None:
        from fluid_build.build_runners.spark._local import clear_failed

        if _CONFIG_HASH.fullmatch(config_hash):
            clear_failed(self.host, self.run, config_hash)

    def retry_hint(self) -> str:
        """How to start a stream whose restarts are spent sooner, when the cause was this
        machine's, not the contract's (the operator's spark-submit or properties file are not
        in the config hash): its run history here is its budget's record."""
        return (
            "; when the cause was this machine's (spark-submit, its properties file), remove "
            f"{self._runs.dir / 'runs'} (the build's run history here) and fluid apply starts "
            "it again"
        )


ADAPTERS = {a.runtime: a for a in (EmrServerless, Glue, EmrEc2, EmrEks, Local)}


def adapter_for(
    run: DesiredRun, clients: Optional[ClientFactory] = None, local_host: Any = None
) -> Adapter:
    """The run's adapter, with the account its names need resolved from the credentials.

    ``local_host`` (a :class:`~._local.LocalHost`) is where a local run lives; unset, the
    working directory."""
    if run.local:
        return Local(run, local_host)
    if run.needs_region or run.needs_account:
        from fluid_build.build_runners.spark._desired import resolve_account, resolve_region

        try:
            if run.needs_region:
                run = run.bind(region=resolve_region())
            if run.needs_account:
                run = run.bind(account=resolve_account(run.region))
        except ValueError as exc:
            raise SparkRunError(str(exc)) from exc
    return ADAPTERS[run.runtime](run, clients or default_clients)
