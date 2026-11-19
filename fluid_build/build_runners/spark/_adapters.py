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

boto3 is the ``[aws]`` extra and is imported only by :func:`default_clients`.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from fluid_build.build_runners.spark._desired import RESTART_FLAG, DesiredRun
from fluid_build.spark import names as _names

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
#: The most of an uploaded job config ``--mode reconcile`` reads (:meth:`Adapter.deployed_job`).
MAX_JOB_BYTES = 1024 * 1024


class SparkRunError(RuntimeError):
    """A run could not be found, started, read or stopped. The message names the run."""


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
    ``config``, a ``botocore.config.Config``, bounds its calls (the posture check's)."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - the [aws] extra is missing
        raise SparkRunError(
            "Spark job runs need boto3: pip install 'data-product-forge[aws]'"
        ) from exc
    return boto3.client(service, region_name=region, config=config)


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


def _conf_params(conf: Mapping[str, str]) -> List[str]:
    out: List[str] = []
    for name in sorted(conf):
        out += ["--conf", f"{name}={conf[name]}"]
    return out


def _submit_parameters(conf: Mapping[str, str]) -> str:
    """``sparkSubmitParameters`` as one string; a value holding a space is quoted."""
    import shlex

    return " ".join(shlex.quote(p) for p in _conf_params(conf))


class Adapter:
    runtime = ""
    service = ""
    #: ``fluid apply`` uploads the run's job config ahead of it (OpenTofu, on AWS), which
    #: ``--mode reconcile`` reads back (:meth:`deployed_job`).
    uploads_job = True

    def __init__(self, run: DesiredRun, clients: Optional[ClientFactory]) -> None:
        self.run = run
        self._clients = clients
        # A local run has no AWS client: its "client" is the machine (``Local.host``).
        self.client = clients(self.service, run.region) if clients is not None else None

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

    def deployed_job(self) -> Optional[str]:
        """The job config the last ``fluid apply`` uploaded (``job.json`` at the run's job
        URI), or None when there is none.

        Raises:
            SparkRunError: it cannot be read (the message names the object and the code).
        """
        bucket, _, key = self.run.job_uri[len("s3://") :].partition("/")
        s3 = (self._clients or default_clients)("s3", self.run.region)
        try:
            # A job config is a few kilobytes: read no more than MAX_JOB_BYTES of it.
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read(MAX_JOB_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 - botocore is the [aws] extra
            error = (getattr(exc, "response", None) or {}).get("Error") or {}
            code = str(error.get("Code") or type(exc).__name__)
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            denied = (
                " (without s3:ListBucket on the bucket, a missing object reads as AccessDenied "
                "too, and with SSE-KMS the identity also needs kms:Decrypt on its key)"
                if code in ("AccessDenied", "403", "Forbidden")
                else ""
            )
            raise SparkRunError(
                f"the job config the last fluid apply uploaded ({self.run.job_uri}) cannot "
                f"be read: {code}; the identity that reconciles needs s3:GetObject on it{denied}"
            ) from exc
        if len(body) > MAX_JOB_BYTES:
            raise SparkRunError(
                f"the job config at {self.run.job_uri} is over {MAX_JOB_BYTES} bytes, more "
                "than forge ever uploads, so it is not the one fluid apply wrote; run fluid apply"
            )
        # Bytes that are not UTF-8 are not forge's job config: they compare as another job.
        return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)

    def account(self) -> str:
        """The AWS account the run is in, from the default credentials."""
        from fluid_build.build_runners.spark._desired import resolve_account

        try:
            return resolve_account(self.run.region)
        except ValueError as exc:
            raise SparkRunError(str(exc)) from exc

    def get(self, run_id: str) -> RunInfo:
        raise NotImplementedError

    def cancel(self, run_id: str) -> None:
        raise NotImplementedError

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
        r = self.run
        request: Dict[str, Any] = {
            "applicationId": self.application_id(),
            "clientToken": token,
            "executionRoleArn": r.role_arn,
            "name": r.run_name,
            "mode": "BATCH" if r.scheduled else "STREAMING",
            "jobDriver": {
                "sparkSubmit": {
                    "entryPoint": r.runner_uri,
                    "entryPointArguments": r.entry_args(restart_of),
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
        r = self.run
        args = r.entry_args(restart_of)
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

    def __init__(self, run: DesiredRun, clients: ClientFactory) -> None:
        super().__init__(run, clients)
        self._clients = clients

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
            iam = self._clients("iam", self.run.region)
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

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        r = self.run
        try:
            self._refuse_unless_attached()
        finally:
            self._cluster_read = None  # a later start reads the cluster again
        args = [
            "spark-submit",
            # Client mode: the driver is the step's own process, so cancelling the step
            # stops the job. In cluster mode the YARN application outlives a cancelled
            # step, and a restart would put a second writer on the checkpoint.
            "--deploy-mode",
            "client",
            *_conf_params(r.spark_conf),
            r.runner_uri,
            *r.entry_args(restart_of),
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
        r = self.run
        request: Dict[str, Any] = {
            "name": r.run_name,
            "virtualClusterId": self.cluster,
            "clientToken": token,
            "executionRoleArn": r.role_arn,
            "releaseLabel": r.release_label,
            "jobDriver": {
                "sparkSubmitJobDriver": {
                    "entryPoint": r.runner_uri,
                    "entryPointArguments": r.entry_args(restart_of),
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
        self._runs = LocalRuns(self.host, run.product, run.build)

    def where(self) -> str:
        return self._runs.where()

    def runs(self, since: Optional[float] = None) -> List[RunInfo]:
        return self._runs.runs()

    def get(self, run_id: str) -> RunInfo:
        return self._runs.get(run_id)

    def start(self, token: str, restart_of: Optional[str] = None) -> str:
        from fluid_build.build_runners.spark._local import start_run
        from fluid_build.spark import runner_source

        return start_run(
            self.host, self.run, token, runner_text=runner_source(), restart_of=restart_of
        )

    def deployed_job(self) -> Optional[str]:
        """None: nothing uploads a local run's job config ahead of it; each start writes the
        one the contract compiles to (:func:`~._local.publish`)."""
        return None

    def cancel(self, run_id: str) -> None:
        self._runs.cancel(run_id)

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
