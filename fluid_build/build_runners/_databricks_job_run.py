# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Start a Databricks dbt job now and wait for it: the build phase of ``fluid apply``.

A dbt build whose ``execution.runtime.platform`` is ``databricks`` runs in the workspace, as
the serverless job ``fluid apply`` provisions (``iac/providers/databricks_jobs.py``). Under
``--mode amend-and-build`` and ``--mode replace-and-build`` the build runner hands such a
build here once ``tofu apply`` has succeeded and the masks and row filters are bound:

1. The job is the one THIS apply's OpenTofu state holds for the build, found by the tags the
   emitter puts on it (``fluid_product``, ``fluid_build``; :func:`jobs_in_state`), never by
   name: a job name is not unique in a workspace, and two environments of one contract
   applied to one workspace each own a job of that name.
2. One run starts now (``POST /api/2.2/jobs/run-now``). It carries an idempotency token, so a
   retried request cannot start a second run, and queueing on, so a run that finds the job
   busy (``max_concurrent_runs`` is 1, and a scheduled run may be going) or the workspace at
   its limit of concurrent tasks waits instead of being skipped. ``replace-and-build`` runs
   the job's own commands with ``--full-refresh`` on ``dbt build`` (the run's
   ``dbt_commands``), as the local runner adds it for that mode. Nothing is written to the
   job: its settings, schedule and pause state stay as they are, so a scheduled job keeps
   its schedule and a job created paused (``DatabricksIacPlugin.plan_notices``) stays paused.
3. The run is polled until it ends (``GET /api/2.2/jobs/runs/get``): its page URL first, then
   a line for every state it enters, and a line a minute while nothing changes. A queued
   run says why: Databricks' reason when it gives one; when it gives none (the usual case,
   seen live), whether it waits behind another run of the job or for the workspace's limit
   on runs or tasks at once (``GET /api/2.2/jobs/runs/list``, active runs of the job).
4. A run that does not end in ``SUCCESS`` fails the build, with its state and message and,
   for each task that did not succeed, the task's ``error`` and the last lines of its output
   (``GET /api/2.2/jobs/runs/get-output``). Every credential value in the environment is
   masked in what is printed, and terminal control sequences are removed.

The wait is capped (:func:`wait_budget`). At the cap, or on Ctrl-C, the run is cancelled
before the build fails, so ``fluid apply`` leaves nothing running that it started (an
interrupted load cancels its INSERT the same way, ``_databricks_load.py``). A continuous job
is never started here: Databricks keeps it running, and a run of it does not end.

Polling follows databricks-sdk-py's run waiter (``JobsAPI.wait_get_run_job_terminated_or_
skipped``: a poll a second longer each time, at most 10 s; Apache-2.0), and the failed-task
report follows Apache Airflow's Databricks provider (``extract_failed_task_errors``: the
latest attempt of each failed task, its ``get-output`` error; Apache-2.0) and
``databricks bundle run``. The client is the provider's (``databricks_enforce.default_client``)
through ``api_client.do``; the SDK is imported only there (the ``[databricks]`` extra).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fluid_build._console import cprint
from fluid_build._console import error as console_error
from fluid_build.util.contract import is_continuous_build, is_databricks_job_build

LOG = logging.getLogger(__name__)

#: The wait cap for one run, in seconds (:func:`wait_budget`).
TIMEOUT_ENV = "FLUID_DATABRICKS_RUN_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 3600
#: Room past the job's own time limits, for the queue and the serverless start.
SLACK_SECONDS = 900
#: A progress line at least this often while the run's state does not change.
HEARTBEAT_SECONDS = 60
#: Lines of a failed task's output that are printed.
TAIL_LINES = 30

_JOBS = "/api/2.2/jobs"
_REPLACE_MODES = ("replace", "replace-and-build")
#: The dbt commands that take ``--full-refresh``.
_FULL_REFRESH_COMMANDS = ("build", "run", "seed")
#: ECMA-48 escape sequences (as ``llm/coding_agent.py`` strips them), then every other C0
#: and C1 control character except tab and newline.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
#: The ``HH:MM:SS`` dbt puts before each line it logs.
_DBT_TIMESTAMP_RE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}\s+")

Say = Callable[[str], None]


@dataclass(frozen=True)
class JobRef:
    """A Databricks job an apply's OpenTofu state holds for one build."""

    job_id: int
    url: str = ""


def started_builds(contract: Mapping[str, Any], build_id: Optional[str]) -> List[Mapping[str, Any]]:
    """The builds a ``-and-build`` apply of ``contract`` starts as Databricks job runs.

    An empty ``build_id`` selects every build, as the build runner's own filter does.
    """
    return [
        build
        for build in contract.get("builds") or []
        if isinstance(build, Mapping)
        and is_databricks_job_build(dict(build))
        and not is_continuous_build(dict(build))
        and (not build_id or build.get("id") == build_id)
    ]


def jobs_in_state(resources: Iterable[Any], product: str) -> Dict[str, List[JobRef]]:
    """Build id to the ``databricks_job`` resources ``product`` has in an OpenTofu state.

    ``resources`` are a state's resources as ``runner.tofu_state_resources`` returns them. A
    list per build, so that a caller can refuse a build with more than one. The tags are the
    emitter's own (``databricks_jobs.PRODUCT_TAG`` / ``BUILD_TAG``).
    """
    from fluid_build.iac.providers.databricks_jobs import BUILD_TAG, PRODUCT_TAG

    found: Dict[str, List[JobRef]] = {}
    for resource in resources:
        if not isinstance(resource, Mapping) or resource.get("type") != "databricks_job":
            continue
        if resource.get("mode", "managed") != "managed":
            continue
        values = resource.get("values") if isinstance(resource.get("values"), Mapping) else {}
        tags = values.get("tags") if isinstance(values.get("tags"), Mapping) else {}
        if tags.get(PRODUCT_TAG) != product or not tags.get(BUILD_TAG):
            continue
        try:
            job_id = int(str(values.get("id")))
        except ValueError:
            continue
        found.setdefault(str(tags[BUILD_TAG]), []).append(
            JobRef(job_id, str(values.get("url") or ""))
        )
    return found


def wait_budget(settings: Mapping[str, Any], env: Mapping[str, str]) -> int:
    """How long ``fluid apply`` waits for one run of a job with ``settings``, in seconds.

    ``FLUID_DATABRICKS_RUN_TIMEOUT_SECONDS`` when it is a positive whole number. Otherwise an
    hour, or, when it is longer, as long as the job may run by its own limits (the job's
    ``timeout_seconds``, else every task's timeout times its attempts plus the retry
    intervals) plus 15 minutes for the queue and the start, so a contract's own
    ``runtime.timeout`` is never cut short. A task without a timeout, or one retried without
    end, leaves the hour.
    """
    raw = str(env.get(TIMEOUT_ENV) or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    own = _whole(settings.get("timeout_seconds"))
    if not own:
        tasks = [task for task in settings.get("tasks") or [] if isinstance(task, Mapping)]
        for task in tasks:
            timeout, retries = _whole(task.get("timeout_seconds")), _whole(task.get("max_retries"))
            if not timeout or retries < 0:
                own = 0
                break
            interval = _whole(task.get("min_retry_interval_millis")) // 1000
            own += timeout * (retries + 1) + interval * retries
    return max(DEFAULT_TIMEOUT_SECONDS, own + SLACK_SECONDS) if own > 0 else DEFAULT_TIMEOUT_SECONDS


def full_refresh_commands(settings: Mapping[str, Any]) -> Optional[List[str]]:
    """The job's dbt commands with ``--full-refresh`` on each that takes it, or ``None``."""
    for task in settings.get("tasks") or []:
        dbt = task.get("dbt_task") if isinstance(task, Mapping) else None
        if not isinstance(dbt, Mapping) or not dbt.get("commands"):
            continue
        commands = []
        for command in dbt["commands"]:
            argv = shlex.split(str(command))
            if (
                argv[:1] == ["dbt"]
                and argv[1:2]
                and argv[1] in _FULL_REFRESH_COMMANDS
                and "--full-refresh" not in argv
            ):
                argv.append("--full-refresh")
            commands.append(shlex.join(argv))
        return commands
    return None


def run_job_build(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    jobs: Mapping[str, Sequence[JobRef]],
    *,
    dry_run: bool = False,
    mode: Optional[str] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Start a run of ``build``'s job now and wait for it to end. Returns 0 or 1.

    ``jobs`` is :func:`jobs_in_state` of the apply that provisioned it.
    """
    from fluid_build.iac.providers.databricks_jobs import BUILD_TAG, PRODUCT_TAG, job_product

    build_id = str(build.get("id") or "unknown")
    product = job_product(contract)
    name = f"fluid {product} {build_id}"
    full_refresh = (mode or "").strip().lower() in _REPLACE_MODES
    with_refresh = " with dbt build --full-refresh" if full_refresh else ""
    if dry_run:
        _say(
            f"\n🔍 [DRY RUN] Build '{build_id}' - would start a run of Databricks job "
            f"'{name}'{with_refresh} and wait for it."
        )
        return 0
    refs = list(jobs.get(build_id) or [])
    if len(refs) != 1:
        found = "no Databricks job" if not refs else f"{len(refs)} Databricks jobs"
        console_error(
            f"Build '{build_id}' - this apply's OpenTofu state holds {found} tagged "
            f"{PRODUCT_TAG}={product}, {BUILD_TAG}={build_id}, so no run was "
            "started. fluid apply provisions the job when it applies the contract to databricks; "
            "check that the contract's provider is databricks, then re-run fluid apply."
        )
        return 1
    try:
        client = (client_factory or _default_client)()
    except ImportError:
        console_error(
            f"Build '{build_id}' - databricks-sdk is not installed, and the run of Databricks job "
            f"'{name}' is started through it. Install the extra: "
            "pip install 'data-product-forge[databricks]'"
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        console_error(
            f"Build '{build_id}' - could not create the Databricks client to start job "
            f"'{name}': {_failure(exc)}"
        )
        return 1
    return start_and_wait(
        client,
        refs[0],
        build_id=build_id,
        name=name,
        full_refresh=full_refresh,
        env=os.environ if env is None else env,
        sleep=sleep,
        clock=clock,
    )


def start_and_wait(
    client: Any,
    job: JobRef,
    *,
    build_id: str,
    name: str,
    full_refresh: bool = False,
    env: Mapping[str, str],
    say: Optional[Say] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run ``job`` now through ``client`` and wait for the run to end. Returns 0 or 1."""
    say = say or _say
    api = client.api_client
    label = f"Databricks job '{name}' ({job.job_id})"
    try:
        settings = _job_settings(api, job.job_id)
        body: Dict[str, Any] = {
            "job_id": job.job_id,
            "idempotency_token": uuid.uuid4().hex,
            "queue": {"enabled": True},
        }
        if full_refresh:
            commands = full_refresh_commands(settings)
            if commands is None:
                console_error(
                    f"Build '{build_id}' - replace-and-build runs dbt with --full-refresh, and "
                    f"{label} has no dbt commands to add it to, so no run was started. Re-run "
                    "fluid apply to bring the job back to what the contract declares."
                )
                return 1
            body["dbt_commands"] = commands
        started = api.do("POST", f"{_JOBS}/run-now", body=body)
        run_id = int((started or {})["run_id"])
    except Exception as exc:  # noqa: BLE001
        console_error(f"Build '{build_id}' - could not start a run of {label}: {_failure(exc)}")
        return 1
    LOG.debug("databricks_job_run_started job_id=%s run_id=%s", job.job_id, run_id)
    say(f"\n🚀 Build '{build_id}' - started run {run_id} of {label}")
    budget = wait_budget(settings, env)
    start = clock()
    try:
        return _wait(api, job.job_id, run_id, build_id, budget, start, say, sleep, clock)
    except KeyboardInterrupt:
        _cancel(api, run_id, say)
        raise


def _wait(
    api: Any,
    job_id: int,
    run_id: int,
    build_id: str,
    budget: int,
    start: float,
    say: Say,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    last: Optional[Tuple[str, str]] = None
    queued_why: Optional[str] = None
    noted = start
    url = ""
    delay = 0
    while True:
        try:
            run = api.do("GET", f"{_JOBS}/runs/get", query={"run_id": run_id}) or {}
        except Exception as exc:  # noqa: BLE001
            console_error(
                f"Build '{build_id}' - could not read run {run_id}: {_failure(exc)}. "
                "It may still be going."
            )
            if url:
                say(f"   run page: {url}")
            return 1
        if not url and run.get("run_page_url"):
            url = clean(run["run_page_url"])
            say(f"   run page: {url}")
        now = clock()
        phase, why = _phase(run)
        if phase == "TERMINATED":
            return _report_end(api, run, run_id, build_id, now - start, url, say)
        if phase != "QUEUED":
            queued_why = None
        elif not why:
            # Databricks gives no reason for most queued runs (seen live, 2026-11-12): name
            # it here, from one read per queued spell.
            if queued_why is None:
                queued_why = _queued_behind(api, job_id, run_id)
            why = queued_why
        if (phase, why) != last:
            say(f"   {_clock(now - start)} {phase}" + (f" - {why}" if why else ""))
            last, noted = (phase, why), now
        elif now - noted >= HEARTBEAT_SECONDS:
            say(f"   {_clock(now - start)} still {phase}")
            noted = now
        if now - start >= budget:
            cancelled = _cancel(api, run_id, say)
            console_error(
                f"Build '{build_id}' - run {run_id} did not end within {_duration(budget)}"
                + (" and was cancelled" if cancelled else "")
                + f". Set {TIMEOUT_ENV} (seconds) to wait longer."
            )
            if url:
                say(f"   run page: {url}")
            LOG.debug("databricks_job_run_timed_out run_id=%s budget=%s", run_id, budget)
            return 1
        delay = min(delay + 1, 10)
        sleep(delay)


def _report_end(
    api: Any,
    run: Mapping[str, Any],
    run_id: int,
    build_id: str,
    elapsed: float,
    url: str,
    say: Say,
) -> int:
    code, message = _outcome(run)
    say(f"   {_clock(elapsed)} TERMINATED {code}")
    LOG.debug("databricks_job_run_ended run_id=%s result=%s", run_id, code)
    if code == "SUCCESS":
        for task_key, task in _latest_attempts(run):
            summary = _dbt_summary(api, task["run_id"])
            if summary:
                say(f"   task '{task_key}': {summary}")
        say(f"✅ Build '{build_id}' - Databricks run {run_id} succeeded in {_duration(elapsed)}")
        return 0
    console_error(
        f"Build '{build_id}' - Databricks run {run_id} ended {code} after {_duration(elapsed)}"
        + (f": {message}" if message else "")
    )
    for task_key, task in _latest_attempts(run):
        if _outcome(task)[0] != "SUCCESS":
            _report_task(api, task_key, task["run_id"], say)
    if url:
        say(f"   run page: {url}")
    return 1


def _queued_behind(api: Any, job_id: int, run_id: int) -> str:
    """Why a run Databricks queued without saying why waits: behind another active run of
    the same job (a job runs one at a time), else for the workspace's limit on runs or tasks
    at once. ``""`` when the runs cannot be listed: the wait goes on either way."""
    try:
        listed = api.do(
            "GET",
            f"{_JOBS}/runs/list",
            query={"job_id": job_id, "active_only": "true", "limit": 25},
        )
    except Exception:  # noqa: BLE001 — the reason is a courtesy
        return ""
    runs = listed.get("runs") if isinstance(listed, Mapping) else None
    others = [
        r.get("run_id")
        for r in runs or []
        if isinstance(r, Mapping) and r.get("run_id") not in (None, run_id)
    ]
    if others:  # newest first: the last one started first
        return f"behind run {others[-1]} of this job, which runs one at a time"
    return (
        "waiting for the workspace's limit on runs or tasks at once "
        "(Databricks Free Edition runs at most 5 job tasks at a time)"
    )


def _dbt_summary(api: Any, task_run: Any) -> str:
    """dbt's own last ``Done. PASS=...`` line of a task's output, or ``""``: what a run that
    succeeded built, at the cost of one read that may fail without failing the build."""
    try:
        output = api.do("GET", f"{_JOBS}/runs/get-output", query={"run_id": task_run}) or {}
    except Exception:  # noqa: BLE001 — the run succeeded; its summary is a courtesy
        return ""
    done = [line for line in clean(output.get("logs") or "").splitlines() if "Done. PASS=" in line]
    return " ".join(_DBT_TIMESTAMP_RE.sub("", done[-1]).split()) if done else ""


def _report_task(api: Any, task_key: str, task_run: Any, say: Say) -> None:
    try:
        output = api.do("GET", f"{_JOBS}/runs/get-output", query={"run_id": task_run}) or {}
    except Exception as exc:  # noqa: BLE001
        say(f"   task '{task_key}': its output could not be read ({_failure(exc)})")
        return
    # One line: Databricks quotes the failed shell command with its newlines escaped.
    error = " ".join(clean(output.get("error") or "").replace("\\n", " ").split())
    say(f"   task '{task_key}'" + (f": {error}" if error else " failed"))
    logs = clean(output.get("logs") or output.get("error_trace") or "")
    lines = [line.rstrip() for line in logs.splitlines() if line.strip()]
    if not lines:
        return
    shown = lines[-TAIL_LINES:]
    of = f"last {len(shown)} of {len(lines)} lines" if len(lines) > len(shown) else "output"
    truncated = " (Databricks kept only the end)" if output.get("logs_truncated") else ""
    say(f"   dbt {of}{truncated}:")
    for line in shown:
        say(f"     {line}")


def _phase(run: Mapping[str, Any]) -> Tuple[str, str]:
    """``(state, why)``: the run's ``status.state`` (its deprecated ``state`` when absent),
    ``TERMINATED`` for every state a run ends in, and why it waits while it is queued."""
    status = run.get("status") if isinstance(run.get("status"), Mapping) else {}
    legacy = run.get("state") if isinstance(run.get("state"), Mapping) else {}
    phase = str(status.get("state") or "").upper()
    if not phase:
        life = str(legacy.get("life_cycle_state") or "PENDING").upper()
        phase = "TERMINATED" if life in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR") else life
    why = ""
    if phase == "QUEUED":
        details = (
            status.get("queue_details") if isinstance(status.get("queue_details"), Mapping) else {}
        )
        message = clean(details.get("message") or legacy.get("queue_reason") or "").strip()
        code = clean(details.get("code") or "").strip()
        why = f"{message} ({code})" if message and code else message or code
    return phase, " ".join(why.split())


def _outcome(run: Mapping[str, Any]) -> Tuple[str, str]:
    """``(code, message)`` of a run that ended: ``SUCCESS`` only when it succeeded."""
    status = run.get("status") if isinstance(run.get("status"), Mapping) else {}
    legacy = run.get("state") if isinstance(run.get("state"), Mapping) else {}
    details = (
        status.get("termination_details")
        if isinstance(status.get("termination_details"), Mapping)
        else {}
    )
    code = str(
        details.get("code")
        or legacy.get("result_state")
        or legacy.get("life_cycle_state")
        or "UNKNOWN"
    ).upper()
    message = clean(details.get("message") or legacy.get("state_message") or "")
    return clean(code).strip(), " ".join(message.split())


def _latest_attempts(run: Mapping[str, Any]) -> List[Tuple[str, Mapping[str, Any]]]:
    """``(task_key, task)`` for the latest attempt of each task of the run."""
    latest: Dict[str, Mapping[str, Any]] = {}
    for task in run.get("tasks") or []:
        if not isinstance(task, Mapping) or task.get("run_id") is None:
            continue
        key = str(task.get("task_key") or task.get("run_id"))
        if key not in latest or _whole(task.get("attempt_number")) >= _whole(
            latest[key].get("attempt_number")
        ):
            latest[key] = task
    return [(clean(key), task) for key, task in latest.items()]


def _job_settings(api: Any, job_id: int) -> Mapping[str, Any]:
    job = api.do("GET", f"{_JOBS}/get", query={"job_id": job_id}) or {}
    settings = job.get("settings") if isinstance(job, Mapping) else None
    return settings if isinstance(settings, Mapping) else {}


def _cancel(api: Any, run_id: int, say: Say) -> bool:
    """Cancel the run; ``False`` (and a line saying so) when the request fails."""
    try:
        api.do("POST", f"{_JOBS}/runs/cancel", body={"run_id": run_id})
    except Exception as exc:  # noqa: BLE001
        say(f"   could not cancel run {run_id}: {_failure(exc)}")
        return False
    say(f"   cancelled run {run_id}")
    LOG.debug("databricks_job_run_cancelled run_id=%s", run_id)
    return True


def clean(text: Any) -> str:
    """``text`` safe to print: escape sequences and control characters removed (tab and
    newline kept), and every credential value in the environment masked."""
    from fluid_build.iac.providers.databricks_auth import credential_values
    from fluid_build.observability.secret_redactor import redact_secret_text

    plain = _CONTROL_RE.sub("", _ANSI_RE.sub("", str(text or "")))
    return redact_secret_text(
        plain, extra_secrets=credential_values(os.environ), keep_dotted_names=True
    )


def _failure(exc: BaseException) -> str:
    from fluid_build.iac.providers.databricks_enforce import scrub

    return f"{type(exc).__name__}: {clean(scrub(exc))}"


def _default_client() -> Any:
    from fluid_build.iac.providers.databricks_enforce import default_client

    return default_client()


def _whole(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clock(seconds: float) -> str:
    """``m:ss`` (``h:mm:ss`` from an hour) since the run started."""
    whole = max(int(seconds), 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _duration(seconds: float) -> str:
    whole = max(int(seconds), 0)
    minutes, secs = divmod(whole, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _say(text: str) -> None:
    # Unwrapped: a run page URL or a log line must survive a copy from a CI log intact.
    cprint(text, markup=False, soft_wrap=True)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "JobRef",
    "TIMEOUT_ENV",
    "clean",
    "full_refresh_commands",
    "jobs_in_state",
    "run_job_build",
    "start_and_wait",
    "started_builds",
    "wait_budget",
]
