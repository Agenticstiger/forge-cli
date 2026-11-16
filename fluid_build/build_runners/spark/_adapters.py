# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One adapter per AWS Spark runtime: find a build's runs, start one, read one, cancel one.

| runtime | start | find | cancel |
|---|---|---|---|
| emr-serverless | ``StartJobRun`` (``mode`` ``STREAMING`` / ``BATCH``, ``retryPolicy``) | ``ListJobRuns`` by name, then ``GetJobRun`` for tags | ``CancelJobRun`` |
| glue | ``StartJobRun`` on the build's Glue job, with run ``Arguments`` | ``GetJobRuns`` | ``BatchStopJobRun`` |
| emr-ec2 | ``AddJobFlowSteps`` (``command-runner.jar spark-submit``) on ``clusterId`` | ``ListSteps`` by name | ``CancelSteps`` (``SEND_INTERRUPT``) |
| emr-eks | emr-containers ``StartJobRun`` on ``virtualClusterId`` | ``ListJobRuns`` by name | ``CancelJobRun`` |

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

from fluid_build.build_runners.spark._desired import DesiredRun

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

#: ``(service, region) -> boto3 client``
ClientFactory = Callable[[str, str], Any]


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


def default_clients(service: str, region: str) -> Any:
    """A boto3 client, from the default credential chain (``AWS_ENDPOINT_URL`` included)."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - the [aws] extra is missing
        raise SparkRunError(
            "Spark job runs need boto3: pip install 'data-product-forge[aws]'"
        ) from exc
    return boto3.client(service, region_name=region)


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

    def __init__(self, run: DesiredRun, clients: ClientFactory) -> None:
        self.run = run
        self.client = clients(self.service, run.region)

    def runs(self) -> List[RunInfo]:  # newest first
        raise NotImplementedError

    def start(self, token: str) -> str:
        raise NotImplementedError

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


class EmrServerless(Adapter):
    runtime, service = "emr-serverless", "emr-serverless"
    #: How far back the reconcile looks for this build's last run.
    LOOKBACK_PAGES = 4

    def __init__(self, run: DesiredRun, clients: ClientFactory) -> None:
        super().__init__(run, clients)
        self._app: Optional[str] = run.target.get("applicationId")

    def application_id(self) -> str:
        if self._app:
            return self._app
        name = self.run.target["applicationName"]
        found = []
        for page in self.client.get_paginator("list_applications").paginate():
            found += [
                a["id"]
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
            run_id=str(job.get("jobRunId") or job.get("id")),
            state=_SERVERLESS.get(raw, PENDING),
            raw_state=raw,
            attempt=job.get("attempt"),
            details=str(job.get("stateDetails") or ""),
            created=_ts(job.get("createdAt")),
            **ident,
        )

    def runs(self) -> List[RunInfo]:
        app = self.application_id()
        mode = "BATCH" if self.run.scheduled else "STREAMING"
        summaries: List[Mapping[str, Any]] = []
        pages = self.client.get_paginator("list_job_runs").paginate(applicationId=app, mode=mode)
        for number, page in enumerate(pages):
            summaries += [
                j for j in page.get("jobRuns") or [] if j.get("name") == self.run.run_name
            ]
            if number + 1 >= self.LOOKBACK_PAGES:
                break
        summaries.sort(key=lambda j: _ts(j.get("createdAt")), reverse=True)
        return [self.get(str(j["id"])) for j in summaries[:10]]

    def get(self, run_id: str) -> RunInfo:
        job = self.client.get_job_run(applicationId=self.application_id(), jobRunId=run_id)
        return self._info(job["jobRun"])

    def start(self, token: str) -> str:
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
                    "entryPointArguments": r.entry_args(),
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

    def runs(self) -> List[RunInfo]:
        page = self.client.get_job_runs(JobName=self.job_name, MaxResults=25)
        runs = [self._info(j) for j in page.get("JobRuns") or []]
        return sorted(runs, key=lambda i: i.created, reverse=True)

    def get(self, run_id: str) -> RunInfo:
        return self._info(self.client.get_job_run(JobName=self.job_name, RunId=run_id)["JobRun"])

    def start(self, token: str) -> str:
        r = self.run
        args = r.entry_args()
        arguments = {args[i]: args[i + 1] for i in range(0, len(args), 2)}
        if r.spark_conf:
            # Glue takes Spark settings as one --conf argument: "a=b --conf c=d".
            pairs = [f"{k}={r.spark_conf[k]}" for k in sorted(r.spark_conf)]
            arguments["--conf"] = " --conf ".join(pairs)
        # Retries are the Glue job's MaxRetries (the infra emit), not a run setting.
        out = self.client.start_job_run(JobName=self.job_name, Arguments=arguments)
        return str(out["JobRunId"])

    def cancel(self, run_id: str) -> None:
        out = self.client.batch_stop_job_run(JobName=self.job_name, JobRunIds=[run_id])
        for error in out.get("Errors") or []:
            detail = (error.get("ErrorDetail") or {}).get("ErrorMessage") or ""
            raise SparkRunError(f"Glue run {run_id} of {self.job_name} was not stopped: {detail}")


# --- EMR on EC2 ----------------------------------------------------------------------

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
            created=_ts((status.get("Timeline") or {}).get("CreationDateTime")),
            **_identity((step.get("Config") or {}).get("Args") or []),
        )

    def runs(self) -> List[RunInfo]:
        page = self.client.list_steps(ClusterId=self.cluster)
        steps = [s for s in page.get("Steps") or [] if s.get("Name") == self.run.run_name]
        return sorted((self._info(s) for s in steps), key=lambda i: i.created, reverse=True)

    def get(self, run_id: str) -> RunInfo:
        return self._info(self.client.describe_step(ClusterId=self.cluster, StepId=run_id)["Step"])

    def start(self, token: str) -> str:
        r = self.run
        args = [
            "spark-submit",
            "--deploy-mode",
            "cluster",
            *_conf_params(r.spark_conf),
            r.runner_uri,
            *r.entry_args(),
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

    def runs(self) -> List[RunInfo]:
        page = self.client.list_job_runs(virtualClusterId=self.cluster, name=self.run.run_name)
        runs = [self._info(j) for j in page.get("jobRuns") or []]
        return sorted(runs, key=lambda i: i.created, reverse=True)

    def get(self, run_id: str) -> RunInfo:
        out = self.client.describe_job_run(id=run_id, virtualClusterId=self.cluster)
        return self._info(out["jobRun"])

    def start(self, token: str) -> str:
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
                    "entryPointArguments": r.entry_args(),
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


ADAPTERS = {a.runtime: a for a in (EmrServerless, Glue, EmrEc2, EmrEks)}


def adapter_for(run: DesiredRun, clients: Optional[ClientFactory] = None) -> Adapter:
    return ADAPTERS[run.runtime](run, clients or default_clients)
