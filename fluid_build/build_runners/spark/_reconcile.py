# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Keep each streaming Spark build's job running with the config the contract declares.

After ``tofu apply`` provisions a Spark build (``fluid apply``), :func:`reconcile` makes its
run match the contract, idempotently:

1. Find the build's runs (:mod:`._adapters`), each marked with the config hash it started
   with.
2. A running job with this hash: nothing to do.
3. A running job with another hash: stop it between two batches, wait until it has stopped,
   and start the new one on the SAME checkpoint, so it resumes where the old one stopped
   (:func:`_stop_between_batches`). The old run is asked through a request beside the job
   config that names it by the stop key it was started with (``--fluid-stop-key``), and
   stops once no batch is running in it; one that has not stopped within
   ``FLUID_SPARK_STOP_GRACE_SECONDS``, or was started without a key, is cancelled. Spark's
   checkpoint holds the offsets of one topic on one cluster, so a new source on the old
   checkpoint is refused before anything is stopped, and the runner refuses at start a
   checkpoint whose offsets name another topic; moving ``properties.stream.checkpoint`` to a
   new prefix is how a stream changes source, and the job then starts from
   ``startingOffsets``. A start the deployed job config or runner would refuse
   (``_adapters.checked_run``) is refused before anything is stopped too. Borrowed: Apache
   Flink's stop-with-savepoint (a job ends at a consistent point before its upgrade) and
   the Flink Kubernetes operator's upgrade, which waits a while for that stop.
4. No running job: start one. When the newest run of this config ended, how it ended
   decides (:func:`_after_end`):

   - cancelled through the runtime (forge's cancel mark on this machine): stopped on
     purpose. A plain ``fluid apply`` starts it again; ``--mode reconcile`` leaves it.
   - FAILED on a runtime that retries runs itself (EMR Serverless, EMR on EKS, Glue, from
     ``execution.retries``): the service's retries are spent, and starting it again on top
     of them would fail the same way, so that run is an error until the build changes.
   - any other end (failed, interrupted, lost with its machine, node or cluster): it died,
     and it is started again on the same checkpoint while fewer than
     ``restarts_per_hour`` of its restarts are under an hour old (EMR on EC2 and local
     runs, which no service retries). Each restart carries ``--fluid-restart-of <run>``,
     so the budget is counted from the runtime's own run history, with no state of forge's.
     Borrowed: Apache Flink's failure-rate restart strategy (at most N failures per
     interval) and systemd's ``Restart=on-failure`` with ``StartLimitBurst`` /
     ``StartLimitIntervalSec`` (a clean stop is never restarted). Unlike both, a spent
     budget is not final: a restart is allowed again once the oldest is an hour old.

``fluid apply --mode reconcile`` (``only``) runs no ``tofu`` and applies no plan: it starts a
stream again only when what the last apply deployed (the job config it uploaded, and the
config hash its runs carry) is what the contract compiles to, and otherwise starts nothing
and tells the operator to run ``fluid apply`` (:func:`_not_as_deployed`).

Two reconciles that start the same stream at once keep the older run: the younger one
cancels its own start (:func:`_oldest`), so one writer stays on the checkpoint.

A scheduled build (batch, or a stream with ``trigger.availableNow``) is never reconciled:
its scheduler starts it through ``fluid apply --mode amend-and-build --build-id <id>``, whose
build phase calls :func:`run_build` and so :func:`run_once`, which waits for the run to end
and cancels it on Ctrl-C, SIGTERM or SIGHUP, as ``_databricks_job_run`` does.

A local run (``runtime: local``, :mod:`._local`) follows the same rules on this machine,
and a reconcile of the whole contract also stops the local runs of a build the contract no
longer runs locally (:func:`stop_removed`).

Polling follows the databricks-sdk run waiter already adapted in ``_databricks_job_run``: a
poll a second longer each time, at most :data:`MAX_POLL_S`.
"""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.build_runners.spark._adapters import (
    CANCELLED,
    CANCELLING,
    FAILED,
    PENDING,
    RUNNING,
    SERVICE_RETRIES,
    SUCCEEDED,
    TERMINAL,
    Adapter,
    ClientFactory,
    RunInfo,
    SparkRunError,
    adapter_for,
    usable_stop_key,
)
from fluid_build.build_runners.spark._desired import (
    PROBATION_ENV,
    RESTART_WINDOW_S,
    STOP_GRACE_ENV,
    DesiredRun,
    config_digest,
)

MAX_POLL_S = 15.0
#: Failed reads in a row before a wait gives up (the run is never cancelled for them).
MAX_FAILED_READS = 5
#: How long a reconcile waits for a cancelled run to stop, and for a started one to leave
#: its pending states; ``FLUID_SPARK_WAIT_SECONDS`` overrides both.
DEFAULT_WAIT_S = 900
#: How long a scheduled run may take before it is cancelled, unless the build says
#: (``execution.runtime.timeout``).
DEFAULT_RUN_WAIT_S = 6 * 3600
WAIT_ENV = "FLUID_SPARK_WAIT_SECONDS"
#: How long a stream asked to stop between batches has before it is cancelled
#: (``STOP_GRACE_ENV``); 0 cancels at once.
DEFAULT_STOP_GRACE_S = 300
#: How long a released stream's new run is watched (``PROBATION_ENV``); 0 keeps it at once. Five
#: minutes, as the Flink Kubernetes operator waits for an upgrade to be stable.
DEFAULT_PROBATION_S = 300


@dataclass
class Outcome:
    """What happened to one build. ``error`` outcomes make ``fluid apply`` fail."""

    build: str
    runtime: str
    # unchanged | started | restarted | scheduled | refused | failed | planned | declined
    # | left-stopped (``--mode reconcile``: cancelled or stopping, so nothing starts)
    action: str
    where: str = ""
    run_id: Optional[str] = None
    state: Optional[str] = None
    message: str = ""
    error: bool = False
    cancelled: List[str] = field(default_factory=list)
    #: The runs of another config that ended once asked to stop between batches.
    stopped: List[str] = field(default_factory=list)

    def line(self) -> str:
        run = f" run {self.run_id}" if self.run_id else ""
        state = f" {self.state}" if self.state else ""
        where = f" on {self.where}" if self.where else ""
        text = f"Spark {self.build} ({self.runtime}): {self.action}{run}{state}{where}"
        return f"{text}. {self.message}" if self.message else text

    def as_dict(self) -> Dict[str, Any]:
        return {
            "build": self.build,
            "runtime": self.runtime,
            "action": self.action,
            "where": self.where,
            "runId": self.run_id,
            "state": self.state,
            "message": self.message,
            "error": self.error,
            "cancelled": list(self.cancelled),
            "stopped": list(self.stopped),
        }


#: C0 and C1 control characters other than whitespace: a runtime's failure text (a step's
#: message, a local driver log) never reaches the terminal with escape sequences in it.
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")


def _clean(text: Any) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    return redact_secret_text(" ".join(_CONTROL.sub("", str(text or "")).split()))[:400]


def wait_budget(env: Optional[Mapping[str, str]] = None) -> float:
    raw = (os.environ if env is None else env).get(WAIT_ENV)
    try:
        value = float(raw) if raw else DEFAULT_WAIT_S
    except ValueError:
        value = DEFAULT_WAIT_S
    return value if value > 0 else DEFAULT_WAIT_S


#: The most a grace or a probation may be (a project's .env may set either): an hour.
MAX_WAIT_SETTING_S = 3600.0


def _seconds(env: Optional[Mapping[str, str]], name: str, default: float) -> float:
    """A number of seconds that ``name`` sets, from 0 to :data:`MAX_WAIT_SETTING_S`; else
    ``default``."""
    raw = (os.environ if env is None else env).get(name)
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        return float(default)
    if not math.isfinite(value) or value < 0:
        return float(default)
    return min(value, MAX_WAIT_SETTING_S)


def stop_grace(env: Optional[Mapping[str, str]] = None) -> float:
    """``FLUID_SPARK_STOP_GRACE_SECONDS``: a number from 0, else :data:`DEFAULT_STOP_GRACE_S`."""
    return _seconds(env, STOP_GRACE_ENV, DEFAULT_STOP_GRACE_S)


def release_probation(env: Optional[Mapping[str, str]] = None) -> float:
    """``FLUID_SPARK_RELEASE_PROBATION_SECONDS``: from 0, else :data:`DEFAULT_PROBATION_S`."""
    return _seconds(env, PROBATION_ENV, DEFAULT_PROBATION_S)


class _Clock:
    def __init__(self, sleep: Callable[[float], None], now: Callable[[], float]) -> None:
        self.sleep, self.now = sleep, now

    def until(
        self, read: Callable[[], RunInfo], done: Callable[[RunInfo], bool], budget: float
    ) -> RunInfo:
        """Poll ``read`` until ``done`` or the budget is spent; the last read either way.

        A read that fails (a throttled call) is tried again; :data:`MAX_FAILED_READS` in a
        row raise the last error, and the run is left as it is (a retried build adopts
        it), as the Databricks job-run waiter does."""
        deadline = self.now() + budget
        delay = 1.0
        info = read()
        failures = 0
        while not done(info) and self.now() < deadline:
            self.sleep(min(delay, max(0.0, deadline - self.now())))
            delay = min(delay + 1.0, MAX_POLL_S)
            try:
                info = read()
                failures = 0
            except Exception:  # noqa: BLE001 - counted; the last one is raised
                failures += 1
                if failures >= MAX_FAILED_READS:
                    raise
        return info


def _token(run: DesiredRun) -> str:
    """A start's idempotency token (EMR Serverless, EMR on EKS: 1-64 ``[A-Za-z0-9._-]``)."""
    return f"fluid-{run.config_hash[:16]}-{uuid.uuid4().hex[:20]}"


def _failure_text(info: RunInfo, limit: int = 400) -> str:
    attempts = f" after {info.attempt} attempts" if info.attempt and info.attempt > 1 else ""
    # A details text can end in "log <file>: " when the log is empty.
    details = _clean(info.details)[:limit].rstrip(": ")
    reason = f": {details}" if details else ""
    return f"run {info.run_id} ended {info.raw_state or info.state}{attempts}{reason}"


def _refuse_new_source(run: DesiredRun, history: Sequence[RunInfo]) -> Optional[str]:
    """Refuse when the newest run that used this checkpoint read another stream."""
    if not (run.checkpoint_id and run.source_id):
        return None
    last = next((h for h in history if h.checkpoint_id == run.checkpoint_id), None)
    if last is None or not last.source_id or last.source_id == run.source_id:
        return None
    return (
        f"the stream changed (topic {run.topic} or its cluster), but checkpoint "
        f"{run.checkpoint} holds the offsets of the stream run {last.run_id} read. Nothing "
        "was cancelled. To read the new stream, set properties.stream.checkpoint to a new "
        "prefix: the job then starts from properties.stream.startingOffsets"
    )


def _oldest(runs: Sequence[RunInfo]) -> List[RunInfo]:
    """The one run kept of several copies of a config: the oldest (ties: the lowest id). It
    does not depend on when a reconcile looked, so two that raced keep the same one."""
    return sorted(runs, key=lambda h: (h.created, h.run_id))[:1]


def _is_copy(index: int, history: Sequence[RunInfo]) -> bool:
    """Whether the cancelled run ``history[index]`` was a second copy of an older run: one
    of its config started from the same run (the same ``restart_of``, or none for a first
    start), with no run of another config started between them. Two applies that restart a
    stream at once start such a pair, and the younger cancels itself."""
    info = history[index]
    for older in history[index + 1 :]:
        if older.config_hash != info.config_hash:
            return False
        if older.restart_of == info.restart_of:
            return True
    return False


def _last_end(history: Sequence[RunInfo]) -> Optional[RunInfo]:
    """The run whose end is the stream's: the newest, passing over a cancelled second copy
    (:func:`_is_copy`), so a death of the run it copied, after it, is not read as a cancel.
    Any other cancelled run is the stream's end, an operator's stop: a cancelled restart
    (another copy may sit between it and the run it restarted), or a run started after
    another config's.

    A run of this config started outside forge with the same restart mark (or none) as an
    older run, then cancelled, is passed over as a copy too: runs carry no end time here."""
    for index, info in enumerate(history):
        if info.state == CANCELLED and _is_copy(index, history):
            continue
        return info
    return None


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _restarts(run: DesiredRun, history: Sequence[RunInfo], now: float) -> List[RunInfo]:
    """This config's restarts of the last :data:`RESTART_WINDOW_S`, oldest first: the runs
    forge started again after one died (``restart_of``). One that was cancelled (the copy a
    concurrent restart cancelled, or an operator's cancel) does not count."""
    since = now - RESTART_WINDOW_S
    found = [
        h
        for h in history
        if h.restart_of
        and h.config_hash == run.config_hash
        and h.created >= since
        and h.state not in (CANCELLED, CANCELLING)
    ]
    return sorted(found, key=lambda h: h.created)


def _spent_text(run: DesiredRun, last: RunInfo, history: Sequence[RunInfo], counted) -> str:
    earlier = [
        h
        for h in history
        if h.config_hash == run.config_hash and h.state in (FAILED, SUCCEEDED) and h is not last
    ][:3]
    restarted = ", ".join(f"{h.run_id} at {_utc(h.created)}" for h in counted[-5:])
    # The next restart is allowed once fewer than the budget are under an hour old: when
    # the budget was lowered (it is not in every config hash), more than it may still count.
    oldest = counted[max(len(counted) - max(run.restarts_per_hour, 1), 0)]
    text = (
        f"{_failure_text(last)}. Not started again: its budget of {run.restarts_per_hour} "
        "restarts per rolling hour (execution.retries.maxAttempts, 5 when unset) is spent: "
        f"{restarted}. It can restart from {_utc(oldest.created + RESTART_WINDOW_S)}, when "
        f"fewer than {run.restarts_per_hour} of them are under an hour old; to start it "
        "sooner, fix the cause and change the build, and fluid apply starts the new config"
    )
    if earlier:
        text += ". Earlier: " + "; ".join(_failure_text(h, 160) for h in earlier)
    return text


def _retried_text(run: DesiredRun, last: RunInfo) -> str:
    fix = "fix the cause and change the build, and fluid apply starts the new config"
    if not run.retry_attempts:
        return (
            f"{_failure_text(last)}. {SERVICE_RETRIES[run.runtime]} only as often as "
            "execution.retries says, and this build sets none, so it was not retried: forge "
            f"does not start a failed run there again. Set execution.retries, or {fix}"
        )
    return (
        f"{_failure_text(last)}. {SERVICE_RETRIES[run.runtime]} from execution.retries "
        f"({run.retry_attempts}), and it stopped retrying: forge does not start it again on "
        f"top of the service's own retries, which would fail the same way; {fix}"
    )


def _job_differs(run: DesiredRun, deployed: bytes) -> bool:
    """Whether the uploaded job config is another job than the one ``run`` (bound to its
    account) compiles to: :meth:`DesiredRun.config_matches`, which puts aside the MSK
    brokers OpenTofu fills in. A stream named by its MSK cluster whose uploaded config names
    no broker at all is another job too: OpenTofu always writes them."""
    import json

    from fluid_build.spark.compiler import MSK_BROKERS_PLACEHOLDER

    if not run.config_matches(deployed):
        return True
    if MSK_BROKERS_PLACEHOLDER not in run.job_text:
        return False
    try:
        source = json.loads(deployed.decode("utf-8")).get("source")
    except (ValueError, AttributeError):
        return True
    servers = source.get("bootstrapServers") if isinstance(source, dict) else None
    return not (isinstance(servers, str) and servers.strip())


def _not_as_deployed(
    run: DesiredRun,
    adapter: Adapter,
    history: Sequence[RunInfo],
    stale: Sequence[RunInfo],
    last: Optional[RunInfo],
) -> Optional[str]:
    """Why ``--mode reconcile`` starts nothing for ``run``: what the last apply deployed (the
    job config it uploaded, the config its runs ran) is not what the contract compiles to,
    or there is nothing deployed. None when it is."""
    apply_hint = "--mode reconcile changes no config: run fluid apply"
    other = [a for a in stale if a.config_hash != run.config_hash]
    if other:
        return (
            f"run {other[0].run_id} of another config ({_clean(other[0].config_hash)[:12] or '?'}) "
            f"is going, and the contract compiles to {run.config_hash[:12]}. {apply_hint}, "
            "which replaces it on the same checkpoint"
        )
    if adapter.uploads_job:
        deployed = adapter.deployed_job()
        uri = adapter.run.job_uri
        if deployed is None:
            return f"no job config at {uri}: the build is not deployed. Run fluid apply"
        if _job_differs(adapter.run, deployed):
            return (
                f"the job config at {uri} is not the one the contract compiles to (another "
                f"apply uploaded it, or it was edited), and a restart would run it. {apply_hint}"
            )
    if not history:
        return (
            f"no run of the build is recorded on {adapter.where()}: --mode reconcile restarts "
            "a stream fluid apply started. Run fluid apply to start it"
        )
    if last is not None and last.config_hash != run.config_hash:
        return (
            f"the last run {last.run_id} ran config {_clean(last.config_hash)[:12] or '?'}, and the "
            f"contract compiles to {run.config_hash[:12]}. {apply_hint} to start the new config"
        )
    return None


@dataclass
class _Start:
    """What starts after an ended run: a restart of ``restart_of``, which ``ended`` says how,
    the ``count`` th of the stream's budget; or, with none of them, the config again."""

    restart_of: Optional[str] = None
    ended: str = ""
    count: str = ""
    #: How the runs of the config it replaces were stopped (:func:`_release_text`).
    release: str = ""

    @property
    def message(self) -> str:
        parts = [self.release] if self.release else []
        if self.restart_of:
            parts.append(f"{self.ended}; started again on the same checkpoint ({self.count})")
        return "; ".join(parts)


def _after_end(
    run: DesiredRun,
    last: RunInfo,
    history: Sequence[RunInfo],
    now: float,
    out: Outcome,
    *,
    only: bool,
    hint: str = "",
) -> Optional[_Start]:
    """The newest run of this config has ended and none is going: what starts now, or None
    when nothing does (``out`` then says why).

    - Cancelled (the runtime's cancel; on this machine, forge's cancel mark): an operator
      stopped it on purpose. ``--mode reconcile`` leaves it stopped; a plain ``fluid apply``
      starts it again, as the operator who applies asks.
    - Failed on a runtime that retries runs itself (:data:`SERVICE_RETRIES`): those retries
      are spent, so nothing starts.
    - Any other end (failed, interrupted, lost with its machine or node, or a stream that
      stopped by itself): it died, and it is started again on the same checkpoint while
      fewer than ``restarts_per_hour`` restarts of it are under an hour old.
    """
    if last.state == CANCELLED:
        if not only:
            return _Start()
        out.action, out.run_id, out.state = "left-stopped", last.run_id, last.state
        out.message = (
            f"run {last.run_id} was cancelled ({last.raw_state}): --mode reconcile leaves a "
            "cancelled stream stopped, and fluid apply starts it again"
        )
        return None
    if last.state == FAILED and run.runtime in SERVICE_RETRIES:
        out.action, out.error, out.run_id, out.state = "failed", True, last.run_id, last.state
        out.message = _retried_text(run, last)
        return None
    counted = _restarts(run, history, now)
    if len(counted) >= run.restarts_per_hour:
        out.action, out.error, out.run_id, out.state = "failed", True, last.run_id, last.state
        out.message = (
            _spent_text(run, last, history, counted)
            if counted
            else f"{_failure_text(last)}. Not started again: the build allows no restart"
        ) + hint
        return None
    return _Start(
        restart_of=last.run_id,
        ended=_failure_text(last),
        count=f"restart {len(counted) + 1} of {run.restarts_per_hour} allowed per rolling hour",
    )


def _reconcile_one(
    run: DesiredRun,
    adapter: Adapter,
    *,
    clock: _Clock,
    budget: float,
    dry_run: bool,
    only: bool = False,
    wall: Callable[[], float] = time.time,
    confirm: Optional[Callable[[str], bool]] = None,
    grace: float = DEFAULT_STOP_GRACE_S,
    probation: float = 0.0,
) -> Outcome:
    out = Outcome(run.build, run.runtime, "unchanged", where=adapter.where())
    now = wall()
    since = now - RESTART_WINDOW_S
    history = adapter.runs(since=since)
    refusal = _refuse_new_source(adapter.run, history)  # bound: its checkpoint names the bucket
    if refusal:
        out.action, out.error, out.message = "refused", True, refusal
        return out
    active = [h for h in history if h.active]
    # The oldest run with this config is kept; any other active run (an old config, or a
    # second copy two concurrent applies started) is a second writer on the checkpoint.
    matching = _oldest(
        [a for a in active if a.config_hash == run.config_hash and a.state != CANCELLING]
    )
    stale = [a for a in active if a not in matching]
    last = None if active else _last_end(history)
    if only and _stops_reconcile_only(run, adapter, history, stale, matching, last, out):
        return out
    if matching and not stale:
        current = matching[0]
        out.run_id, out.state = current.run_id, current.state
        if current.attempt and current.attempt > 1:
            out.message = f"attempt {current.attempt}: the job has restarted after failures"
        if not only and not dry_run and current.state == RUNNING:
            _keep_record(adapter, out)  # a start that was still pending had none kept
        return out
    start = _Start()
    if last is not None and last.config_hash == run.config_hash:
        if only and last.state == CANCELLED:
            # A cluster that ended cancels the steps still pending on it: a stream there is
            # reported as the cluster's, not left stopped as an operator's.
            adapter.check_target()
        hint = getattr(adapter, "retry_hint", None)
        decided = _after_end(
            run, last, history, now, out, only=only,
            hint=hint() if callable(hint) else "",
        )  # fmt: skip
        if decided is None:
            return out
        start = decided
    # With the new config already writing the checkpoint, a run of another config there is a
    # second writer: it is cancelled at once, never asked and waited for.
    grace = 0.0 if matching else grace
    if dry_run:
        return _planned(run, out, stale, matching, start, grace)
    if only and confirm is not None and not confirm(_question(run, out, stale, matching, start)):
        out.action, out.message = "declined", _declined_text(stale, start)
        return out
    if stale and not matching and not _checked_first(adapter, stale, out):
        return out
    left, notes = _stop_between_batches(run, adapter, stale, out, clock, grace)
    if not _cancel_and_wait(adapter, left, out, clock, budget):
        return out
    if matching:  # the new config already runs; the stale runs were extra
        out.run_id, out.state = matching[0].run_id, matching[0].state
        return out
    start.release = _release_text(run, stale or ([last] if last else []), notes, history)
    out = _start(run, adapter, start, out, clock=clock, budget=budget, since=since)
    replaced = [s for s in stale if s.config_hash != run.config_hash]
    if not replaced and last is not None and last.config_hash != run.config_hash:
        # The previous config ended (a run that died, or one a release stopped): a new config
        # that fails goes back to it; an operator's cancel is no config to go back to.
        replaced = [] if last.state == CANCELLED else [last]
    return _after_start(
        adapter,
        out,
        replaced[0] if replaced else None,
        only=only,
        clock=clock,
        budget=budget,
        window=probation,
    )


def _after_start(
    adapter: Adapter,
    out: Outcome,
    last_good: Optional[RunInfo],
    *,
    only: bool = False,
    clock: _Clock,
    budget: float,
    window: float,
) -> Outcome:
    """What follows a start: the started run's release record is kept, for a later release to
    roll back to; and when the start released a new config over ``last_good``, the run that
    replaced it is watched for ``window`` seconds (:func:`_probation`), and one that fails is
    replaced by ``last_good``'s config again (:func:`_roll_back`)."""
    if out.action not in ("started", "restarted") or out.run_id is None or only:
        return out  # --mode reconcile keeps no record (its role may not write one) and rolls back nothing
    if out.state == RUNNING:
        _keep_record(adapter, out)
    if last_good is None or window <= 0:
        return out
    new_id = str(out.run_id)
    if out.state in (FAILED, SUCCEEDED):  # it ended at once: the outcome already says how
        failed: Optional[RunInfo] = RunInfo(new_id, str(out.state), str(out.state))
        head = out.message
    elif out.state == RUNNING:
        try:
            failed = _probation(adapter, out, clock, window)
        except Exception as exc:  # noqa: BLE001 - the run is going: say so with the error
            out.error = True
            out.message = _joined(
                out.message,
                f"checking on run {new_id} during its probation failed: {_error_text(exc)}. It "
                "is left going, and the next fluid apply reports it",
            )
            return out
        if failed is None:
            out.message = _joined(out.message, f"it ran through its probation ({int(window)} s)")
            return out
        if failed.state in (CANCELLED, SUCCEEDED):
            # Stopped from outside (an operator's cancel, another fluid apply's stop): no failure
            # of the new config, and no rollback against that stop.
            out.state = failed.state
            out.message = _joined(
                out.message,
                f"run {new_id} was stopped during its probation ({failed.raw_state}), so nothing "
                "was rolled back",
            )
            return out
        if failed.state in TERMINAL:
            reason = _failure_text(failed)
        else:
            reason = f"run {new_id} was retried by the runtime (attempt {failed.attempt})"
        head = _joined(out.message, f"run {new_id} failed its probation: {reason}")
    else:  # still starting or cancelled at once: the next fluid apply reports it
        return out
    return _roll_back(adapter, out, failed, last_good, head, clock=clock, budget=budget)


def _joined(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def _keep_record(adapter: Adapter, out: Outcome) -> None:
    """Keep the started run's job config as a release record; a record that cannot be kept is a
    note: the run is going, and only a later rollback to it needs the record."""
    try:
        adapter.keep_release(adapter.checked())
    except Exception as exc:  # noqa: BLE001 - a note, never the start's error
        out.message = _joined(
            out.message,
            f"no release record was kept ({_error_text(exc)}), so a later release cannot roll "
            "back to this config",
        )


def _probation(adapter: Adapter, out: Outcome, clock: _Clock, window: float) -> Optional[RunInfo]:
    """Watch the new run for ``window`` seconds: the read that shows it failed (it ended, or the
    service retried it, a higher attempt than it started with), or None when it ran through.
    Borrowed: the Flink Kubernetes operator rolls an upgrade back unless the job is seen running
    within ``kubernetes.operator.deployment.readiness.timeout``; a stream that reaches RUNNING
    often fails in its first batches, so forge watches the whole window."""
    run_id = str(out.run_id)
    first = adapter.get(run_id)
    base = first.attempt or 1

    def failed(seen: RunInfo) -> bool:
        return seen.state in TERMINAL or (seen.attempt or 1) > base

    if failed(first):
        return first
    seen = clock.until(lambda: adapter.get(run_id), failed, window)
    return seen if failed(seen) else None


def _roll_back(
    adapter: Adapter,
    out: Outcome,
    failed: RunInfo,
    last_good: RunInfo,
    head: str,
    *,
    clock: _Clock,
    budget: float,
) -> Outcome:
    """Replace the new run that failed its probation by ``last_good``'s config: its job config
    from the release record (checked against the digest the runtime recorded when forge started
    ``last_good``), with the runner and settings this apply compiled, on its checkpoint. The
    outcome is an error either way: the release did not hold."""
    new_id = str(out.run_id)
    older = _clean(last_good.config_hash)[:12] or "?"
    out.action, out.error = "failed", True
    current = adapter.checked()
    if last_good.job_sha256 and last_good.job_sha256 == _job_digest(current):
        out.message = (
            f"{head}; the release changed only the run's settings or runner, not its job config, "
            "and forge rolls back the job config only, so nothing was rolled back. Fix the "
            "contract and run fluid apply"
        )
        return out
    text, why = _record_text(adapter, last_good)
    if text is None:
        out.message = (
            f"{head}; config {older} of run {last_good.run_id} cannot be started again: {why}. "
            "Fix the contract and run fluid apply"
        )
        return out
    try:
        if failed.state not in TERMINAL:
            adapter.cancel(new_id)
            out.cancelled.append(new_id)
            ended = clock.until(lambda: adapter.get(new_id), lambda i: i.state in TERMINAL, budget)
            if ended.state not in TERMINAL:
                out.message = (
                    f"{head}; it was cancelled but is still {ended.raw_state} after {int(budget)} s "
                    f"({WAIT_ENV}), so config {older} was not started again"
                )
                return out
        others = [h for h in adapter.runs() if h.active and h.run_id != new_id]
        if others:  # another fluid apply has started a run since: one writer on the checkpoint
            out.message = (
                f"{head}; run {others[0].run_id} is going on the checkpoint (another fluid apply), "
                f"so config {older} was not started again"
            )
            return out
        back = _rollback_run(current, last_good, text)
        again = adapter.sibling(back)
        rb_id = again.start(_token(back))
    except Exception as exc:  # noqa: BLE001 - the rollback's error is the outcome, with the rest
        out.message = f"{head}; rolling back to config {older} failed: {_error_text(exc)}"
        return out
    out.action, out.run_id, out.state = "rolled-back", rb_id, None
    try:
        rb = clock.until(lambda: again.get(rb_id), lambda i: i.state != PENDING, budget)
    except Exception as exc:  # noqa: BLE001 - the rollback run is going: say so with the error
        out.message = (
            f"{head}; rolled back to config {older}: run {rb_id} was started from its release "
            f"record, but checking on it failed: {_error_text(exc)}"
        )
        return out
    out.state = rb.state
    out.message = (
        f"{head}; rolled back to config {older}: run {rb_id} started from its release record on "
        "its checkpoint"
    )
    if rb.state in TERMINAL:
        out.message += f", and {_failure_text(rb)}"
    return out


def _record_text(adapter: Adapter, last_good: RunInfo) -> Tuple[Optional[str], str]:
    """The last good config's job config from its release record, once its digest is the one
    the runtime recorded for ``last_good`` (``--fluid-job-sha256``); else None and why."""
    try:
        body = adapter.release_config(last_good)
    except Exception as exc:  # noqa: BLE001 - why it cannot roll back
        return None, _error_text(exc)
    if body is None:
        return None, (
            "no release record of its job config (it was started by an older fluid apply, or "
            "the record was not kept)"
        )
    try:
        text = body.decode("utf-8")
        matches = config_digest(text) == last_good.job_sha256
    except (UnicodeDecodeError, ValueError):
        matches = False
    if not matches:
        return None, f"its release record is not the job config run {last_good.run_id} ran"
    return text, ""


def _job_digest(run: DesiredRun) -> str:
    """The digest of the job config ``run`` starts with (a local run's is not read back)."""
    return run.job_sha256 or config_digest(run.job_text)


def _rollback_run(run: DesiredRun, last_good: RunInfo, text: str) -> DesiredRun:
    """``run`` (this apply's, checked) with ``last_good``'s job config: its record as the job
    config, its identity, and the checkpoint and topic that config names."""
    import dataclasses
    import hashlib
    import json

    from fluid_build.build_runners.spark._adapters import release_config_uri

    config = json.loads(text)
    source = config.get("source") if isinstance(config.get("source"), Mapping) else {}
    # A hash of its own: the old job config with this apply's runner and settings is neither
    # config, so a contract put back to the old config replaces it with the old settings too.
    label = hashlib.sha256(f"rollback:{last_good.config_hash}:{run.config_hash}".encode())
    return dataclasses.replace(
        run,
        config_hash=label.hexdigest(),
        # The failed release's own stamps name a release this run is not; the run id stays.
        product_version=None,
        contract_digest=None,
        bundle_digest=None,
        release_id=None,
        source_id=last_good.source_id,
        checkpoint_id=last_good.checkpoint_id,
        job_uri=release_config_uri(run.job_uri, str(last_good.job_sha256)),
        job_text=text,
        job_sha256=last_good.job_sha256,
        checkpoint=config.get("checkpoint"),
        topic=source.get("topic") if isinstance(source, Mapping) else None,
    )


def _stops_reconcile_only(
    run: DesiredRun,
    adapter: Adapter,
    history: Sequence[RunInfo],
    stale: Sequence[RunInfo],
    matching: Sequence[RunInfo],
    last: Optional[RunInfo],
    out: Outcome,
) -> bool:
    """Whether ``--mode reconcile`` does nothing more for ``run`` (``out`` then says why):
    the deployment is not the contract (:func:`_not_as_deployed`), or this config's run is
    being stopped and is left to stop."""
    problem = _not_as_deployed(run, adapter, history, stale, last)
    if problem:
        out.action, out.error, out.message = "refused", True, problem
        return True
    if stale and not matching:
        out.action, out.run_id, out.state = "left-stopped", stale[0].run_id, stale[0].state
        out.message = (
            f"run {stale[0].run_id} is being stopped ({stale[0].raw_state}): --mode "
            "reconcile starts nothing while it stops, and looks again next time"
        )
        return True
    return False


def _planned(
    run: DesiredRun,
    out: Outcome,
    stale: Sequence[RunInfo],
    matching: Sequence[RunInfo],
    start: _Start,
    grace: float,
) -> Outcome:
    out.action = "planned"
    if stale:
        out.message = f"would {_stop_plan(run, stale, grace)}" + (
            "" if matching else " and start the new config"
        )
    elif start.restart_of:
        out.message = f"would start it again on the same checkpoint: {start.ended} ({start.count})"
    else:
        out.message = "would start the job"
    return out


def _declined_text(stale: Sequence[RunInfo], start: _Start) -> str:
    if stale:
        return f"declined: {', '.join(s.run_id for s in stale)} not cancelled, nothing started"
    return f"declined: not restarted, the stream stays stopped ({start.ended})"


def _question(
    run: DesiredRun,
    out: Outcome,
    stale: Sequence[RunInfo],
    matching: Sequence[RunInfo],
    start: _Start,
) -> str:
    if stale:
        what = (
            f"cancel {', '.join(s.run_id for s in stale)}, a second copy of run "
            f"{matching[0].run_id}"
        )
    else:
        what = f"{start.ended}. Start it again on its checkpoint ({start.count})"
    return f"Spark {run.build} ({run.runtime}) on {out.where}: {what}?"


def _checked_first(adapter: Adapter, stale: Sequence[RunInfo], out: Outcome) -> bool:
    """Read back the job config and runner the new run will start on
    (:meth:`Adapter.checked`) before ``stale``, the runs of another config, are cancelled;
    False (``out`` refused) when another apply has changed them: the old runs keep going."""
    try:
        adapter.checked()
    except SparkRunError as exc:
        ids = ", ".join(s.run_id for s in stale)
        they = f"run {ids} was" if len(stale) == 1 else f"runs {ids} were"
        out.action, out.error = "refused", True
        out.message = f"{_clean(exc)}; {they} left running"
        return False
    return True


def _askable(run: DesiredRun, info: RunInfo, grace: float) -> bool:
    """Whether ``info`` is asked to stop between batches rather than cancelled: a run of
    another config (a second copy of this one is a second writer, cancelled at once) that
    was started with a stop key, while the operator allows a grace."""
    return (
        grace > 0
        and info.config_hash != run.config_hash
        and info.state != CANCELLING
        and usable_stop_key(info.stop_key)
    )


def _stop_plan(run: DesiredRun, stale: Sequence[RunInfo], grace: float) -> str:
    """What a dry run says it would do with ``stale``."""
    asked = [s.run_id for s in stale if _askable(run, s, grace)]
    cancelled = [s.run_id for s in stale if not _askable(run, s, grace)]
    parts = []
    if asked:
        parts.append(
            f"stop {', '.join(asked)} between batches (cancelling a run still going after "
            f"{int(grace)} s)"
        )
    if cancelled:
        parts.append(f"cancel {', '.join(cancelled)}")
    return " and ".join(parts)


def _stop_between_batches(
    run: DesiredRun,
    adapter: Adapter,
    stale: Sequence[RunInfo],
    out: Outcome,
    clock: _Clock,
    grace: float,
    what: str = "run",
) -> Tuple[List[RunInfo], List[str]]:
    """Ask each run of ``stale`` that can be asked (:func:`_askable`) to stop between
    batches, and wait up to ``grace`` seconds for them to end (``out.stopped``). Returns the
    runs left to cancel (the others, and those still going) and what happened to each run of
    another config, each named ``what <id>``. The requests are withdrawn whatever happens, a
    Ctrl-C included: a run that has not acted on its request yet keeps going."""
    left: List[RunInfo] = []
    notes: List[str] = []
    requests: List[Tuple[RunInfo, str]] = []
    written: List[RunInfo] = []
    try:
        for old in stale:
            if not _askable(run, old, grace):
                if grace > 0 and old.config_hash != run.config_hash and old.state != CANCELLING:
                    notes.append(
                        f"{what} {old.run_id} was cancelled: it was started without a stop key "
                        "(by an older fluid apply), so it cannot be asked to stop between batches"
                    )
                left.append(old)
                continue
            try:
                uri = adapter.stop_request_for(old)
            except SparkRunError as exc:
                notes.append(f"{what} {old.run_id} was cancelled: {_clean(exc)}")
                left.append(old)
                continue
            if uri is None:
                left.append(old)
                continue
            # Withdrawn whatever happens from here, a Ctrl-C during the write included.
            requests.append((old, uri))
            try:
                adapter.put_stop_request(uri)
            except SparkRunError as exc:
                notes.append(f"{what} {old.run_id} was cancelled: {_clean(exc)}")
                left.append(old)
                continue
            written.append(old)
        deadline = clock.now() + grace
        for old in written:
            ended = clock.until(
                partial(adapter.get, old.run_id),
                lambda i: i.state in TERMINAL,
                max(0.0, deadline - clock.now()),
            )
            if ended.state == SUCCEEDED:
                out.stopped.append(old.run_id)
                notes.append(f"{what} {old.run_id} stopped between batches")
            elif ended.state in TERMINAL:
                out.stopped.append(old.run_id)
                notes.append(f"{what} {old.run_id} ended {ended.raw_state} as it was asked to stop")
            else:
                notes.append(
                    f"{what} {old.run_id} did not stop between batches within {int(grace)} s "
                    f"({STOP_GRACE_ENV}) and was cancelled"
                )
                left.append(old)
    finally:
        interrupted: Optional[BaseException] = None
        for _old, uri in requests:
            try:
                adapter.withdraw_stop(uri)
            except KeyboardInterrupt as exc:  # a second Ctrl-C: withdraw the rest, then stop
                interrupted = exc
        if interrupted is not None:
            raise interrupted
    return left, notes


def _release_text(
    run: DesiredRun,
    previous: Sequence[RunInfo],
    notes: Sequence[str],
    history: Sequence[RunInfo] = (),
) -> str:
    """The configs a start moves the stream between, how the old runs stopped, and, when the
    contract moved the checkpoint, where the new run starts reading: what the checkpoint holds
    decides (Spark resumes a position it finds), and forge sees only the runs' history."""
    older = [p for p in previous if p.config_hash != run.config_hash]
    if not older:
        return "; ".join(notes)
    hashes = sorted({_clean(p.config_hash)[:12] or "?" for p in older})
    parts = [f"config {', '.join(hashes)} -> {run.config_hash[:12]}", *notes]
    if run.checkpoint_id and any(
        p.checkpoint_id and p.checkpoint_id != run.checkpoint_id for p in older
    ):
        used = next((h for h in history if h.checkpoint_id == run.checkpoint_id), None)
        if used is not None:
            parts.append(
                f"the checkpoint moved back to one run {used.run_id} used "
                "(properties.stream.checkpoint): the new run resumes the position it holds"
            )
        else:
            parts.append(
                "the checkpoint moved (properties.stream.checkpoint): the new run starts from "
                "properties.stream.startingOffsets unless it already holds a position, and the "
                "old checkpoint is left as it is"
            )
    return "; ".join(parts)


def _cancel_and_wait(
    adapter: Adapter, stale: Sequence[RunInfo], out: Outcome, clock: _Clock, budget: float
) -> bool:
    """Cancel ``stale`` and wait until each has stopped; False (``out`` failed) when one is
    still going after ``budget``, or a cancel or a read failed (``out`` names what was
    cancelled before it)."""
    try:
        for old in stale:
            if old.state != CANCELLING:
                adapter.cancel(old.run_id)
            out.cancelled.append(old.run_id)
        waited = [
            (
                old_id,
                clock.until(lambda i=old_id: adapter.get(i), lambda i: i.state in TERMINAL, budget),
            )
            for old_id in out.cancelled
        ]
    except Exception as exc:  # noqa: BLE001 - the AWS error is the outcome, with what it did
        out.action, out.error = "failed", True
        out.message = _error_text(exc) + (
            f"; cancelled {', '.join(out.cancelled)} before it, and started nothing"
            if out.cancelled
            else ""
        )
        return False
    for old_id, ended in waited:
        if ended.state not in TERMINAL:
            out.action, out.error, out.run_id, out.state = "failed", True, old_id, ended.state
            out.message = (
                f"run {old_id} was cancelled but is still {ended.raw_state} after "
                f"{int(budget)} s ({WAIT_ENV}); nothing new was started, and fluid apply "
                "starts it once the old run has stopped"
            )
            return False
    return True


def _start(
    run: DesiredRun,
    adapter: Adapter,
    start: _Start,
    out: Outcome,
    *,
    clock: _Clock,
    budget: float,
    since: float,
) -> Outcome:
    """Start the run (``start`` says whether it is a restart) and wait until it has left its
    pending states."""
    adapter.check_target()
    run_id = adapter.start(_token(run), restart_of=start.restart_of)
    out.action = "restarted" if out.cancelled or out.stopped or start.restart_of else "started"
    out.run_id = run_id
    try:
        # Two reconciles that found the stream dead at once both started it: the younger
        # run stops itself, so one writer stays on the checkpoint (both keep the oldest).
        first = _oldest(
            [
                h
                for h in adapter.runs(since=since)
                if h.active and h.config_hash == run.config_hash and h.state != CANCELLING
            ]
        )
        if first and first[0].run_id != run_id:
            adapter.cancel(run_id)
            out.cancelled.append(run_id)
            out.action, out.run_id, out.state = "unchanged", first[0].run_id, first[0].state
            out.message = (
                f"run {first[0].run_id} of this config started first (another fluid apply at "
                f"the same time); run {run_id}, which this one started, was cancelled"
            )
            return out
        started = clock.until(lambda: adapter.get(run_id), lambda i: i.state != PENDING, budget)
    except Exception as exc:  # noqa: BLE001 - the run is going: say so with the error
        out.error = True
        out.message = (
            f"{start.message + '; ' if start.message else ''}run {run_id} was started, but "
            f"checking on it failed: {_error_text(exc)}. It is left going, and the next "
            "reconcile reports it"
        )
        return out
    out.run_id, out.state = run_id, started.state
    out.message = start.message
    if started.state in TERMINAL:
        # A long-running job that ends at once failed, whatever it reports.
        out.error = True
        out.message = f"{start.message + '; ' if start.message else ''}{_failure_text(started)}"
    elif started.state == PENDING:
        out.message = (
            f"{start.message + '; ' if start.message else ''}still starting after "
            f"{int(budget)} s; the next fluid apply reports it"
        )
    return out


def reconcile(
    runs: Sequence[DesiredRun],
    *,
    clients: Optional[ClientFactory] = None,
    local_host: Any = None,
    dry_run: bool = False,
    only: bool = False,
    confirm: Optional[Callable[[str], bool]] = None,
    stop_removed_of: Optional[Tuple[str, Collection[str]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    env: Optional[Mapping[str, str]] = None,
) -> List[Outcome]:
    """Reconcile every long-running build in ``runs``; scheduled builds are reported only.

    ``only`` is ``fluid apply --mode reconcile``: it starts a stream again only when what
    the last apply deployed is what the contract compiles to (:func:`_not_as_deployed`),
    never replaces a config, never touches a scheduled build, and leaves a cancelled stream
    stopped; ``confirm`` is asked before it starts or cancels anything.

    An AWS error on one build is that build's error outcome; the others still reconcile.
    ``stop_removed_of`` (product id, the builds it runs locally) also stops this machine's
    runs of any other build of that product (:func:`stop_removed`); never with ``only``.
    """
    clock, budget, grace = _Clock(sleep, now), wait_budget(env), stop_grace(env)
    probation = release_probation(env)
    outcomes = []
    for run in runs:
        if run.scheduled and (only or not run.checkpoint):
            outcomes.append(_scheduled(run))
            continue
        try:
            if run.scheduled:  # an availableNow stream: stop a job left from a continuous one
                adapter = adapter_for(run, clients, local_host)
                outcomes.append(_stop_continuous(run, adapter, clock, budget, dry_run, grace))
                continue
            adapter = adapter_for(run, clients, local_host)
            outcomes.append(
                _reconcile_one(
                    run,
                    adapter,
                    clock=clock,
                    budget=budget,
                    dry_run=dry_run,
                    only=only,
                    wall=wall,
                    confirm=confirm,
                    grace=grace,
                    probation=0.0 if only else probation,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one build's AWS error is its outcome
            outcomes.append(_error_outcome(run, exc))
    if stop_removed_of is not None and not only:
        product, kept = stop_removed_of
        outcomes.extend(
            stop_removed(product, kept, local_host, clock=clock, budget=budget, dry_run=dry_run)
        )
    return outcomes


def stop_removed(
    product: str,
    kept: Collection[str],
    local_host: Any,
    *,
    clock: Optional[_Clock] = None,
    budget: Optional[float] = None,
    dry_run: bool = False,
) -> List[Outcome]:
    """Stop this machine's runs of ``product``'s builds other than ``kept``: a build the
    contract no longer declares, or no longer runs locally, must not keep writing.

    Reads the run directories only (``.fluid/<product>/runtime/spark/``), so it runs on a
    machine with no local run at all, and a dry run changes nothing."""
    from fluid_build.build_runners.spark._local import (
        LocalRuns,
        builds_with_runs,
        default_host,
    )

    host = local_host if local_host is not None else default_host()
    clock = clock or _Clock(time.sleep, time.monotonic)
    budget = wait_budget() if budget is None else budget
    outcomes = []
    for build in builds_with_runs(host, product):
        if build in kept:
            continue
        local = LocalRuns(host, product, build)
        out = Outcome(build, "local", "stopped", where=local.where())
        try:
            active = [h for h in local.runs() if h.active]
            if not active:
                continue
            if dry_run:
                out.action = "planned"
                out.message = (
                    f"would stop {', '.join(h.run_id for h in active)}: the contract no longer "
                    "runs this build here"
                )
                outcomes.append(out)
                continue
            for h in active:
                if h.state != CANCELLING:
                    local.cancel(h.run_id)
                out.cancelled.append(h.run_id)
                ended = clock.until(
                    lambda i=h.run_id: local.get(i), lambda i: i.state in TERMINAL, budget
                )
                if ended.state not in TERMINAL:
                    out.action, out.error, out.run_id = "failed", True, h.run_id
                    out.message = f"run {h.run_id} was cancelled but is still {ended.raw_state}"
                    break
            else:
                out.run_id, out.state = out.cancelled[0], CANCELLED
                out.message = "the contract no longer runs this build here"
        except Exception as exc:  # noqa: BLE001 - one build's error is its outcome
            out.action, out.error, out.message = "failed", True, _clean(exc)
        outcomes.append(out)
    return outcomes


def _scheduled(run: DesiredRun) -> Outcome:
    return Outcome(
        run.build,
        run.runtime,
        "scheduled",
        message=(
            "a scheduler starts it: fluid apply <contract> --mode amend-and-build "
            f"--build-id {run.build}"
        ),
    )


def _stop_continuous(
    run: DesiredRun,
    adapter: Adapter,
    clock: _Clock,
    budget: float,
    dry_run: bool,
    grace: float = DEFAULT_STOP_GRACE_S,
) -> Outcome:
    """A stream that moved to ``trigger.availableNow``: its scheduler starts it from now on,
    so an active run with another config (the old continuous job) is stopped between batches
    (:func:`_stop_between_batches`; cancelled when it does not stop), or the scheduled runs
    would share its checkpoint."""
    out = _scheduled(run)
    out.where = adapter.where()
    active = [h for h in adapter.runs() if h.active]
    old = [h for h in active if h.config_hash != run.config_hash]
    if not old:
        return out
    if any(h.config_hash == run.config_hash for h in active):
        grace = 0.0  # a scheduled run of this config already writes the checkpoint
    if dry_run:
        out.message += f"; would {_stop_plan(run, old, grace)}"
        return out
    left, notes = _stop_between_batches(run, adapter, old, out, clock, grace, "the continuous run")
    if notes:
        out.message += "; " + "; ".join(notes)
    for h in left:
        if h.state != CANCELLING:
            adapter.cancel(h.run_id)
        out.cancelled.append(h.run_id)
        ended = clock.until(
            lambda i=h.run_id: adapter.get(i), lambda i: i.state in TERMINAL, budget
        )
        if ended.state not in TERMINAL:
            out.action, out.error, out.run_id, out.state = "failed", True, h.run_id, ended.state
            out.message = f"run {h.run_id} was cancelled but is still {ended.raw_state}"
            return out
    if out.cancelled and not notes:  # a grace of 0: nothing was asked
        out.message += f"; cancelled the continuous run {', '.join(out.cancelled)}"
    return out


def _error_text(exc: BaseException) -> str:
    """An error's text for an outcome: its AWS error code first, redacted."""
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        code = str((response.get("Error") or {}).get("Code") or "")
    text = _clean(exc) if isinstance(exc, SparkRunError) else f"{type(exc).__name__}: {_clean(exc)}"
    return f"{code + ': ' if code and code not in text else ''}{text}"


def _error_outcome(run: DesiredRun, exc: BaseException) -> Outcome:
    return Outcome(run.build, run.runtime, "failed", message=_error_text(exc), error=True)


def run_once(
    run: DesiredRun,
    *,
    clients: Optional[ClientFactory] = None,
    local_host: Any = None,
    timeout: float = DEFAULT_RUN_WAIT_S,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Outcome:
    """One run of a scheduled build, for its scheduler (:func:`run_build`): start it and wait
    for it to end. The outcome is an error unless it SUCCEEDED. Ctrl-C, SIGTERM or SIGHUP
    while it is waited on cancel it, and the outcome is ``cancelled`` (an error).

    Raises:
        ValueError: ``run`` is a stream; ``fluid apply`` keeps it running (:func:`reconcile`).
    """
    from fluid_build.build_runners._databricks_job_run import interrupt_guard

    if not run.scheduled:
        raise ValueError(
            f"Spark build {run.build} is a stream that fluid apply keeps running; it has no "
            "run to start and wait for"
        )
    started: List[str] = []
    try:
        adapter = adapter_for(run, clients, local_host)
        out = Outcome(run.build, run.runtime, "started", where=adapter.where())

        def cancel() -> None:
            if not started:
                return
            try:
                adapter.cancel(started[0])
                out.cancelled.append(started[0])
                say(f"Spark {run.build}: cancelled run {started[0]}")
            except Exception as exc:  # noqa: BLE001 - say it; the interrupt goes on
                say(f"Spark {run.build}: could not cancel run {started[0]}: {_clean(exc)}")

        clock = _Clock(sleep, now)
        try:
            # Installed before the start: a SIGTERM right after it still cancels the run.
            with interrupt_guard(cancel):
                # A retried task (its worker killed before it could cancel) finds its run
                # going: wait for that one rather than start a second writer of the table.
                listed = [h for h in adapter.runs() if h.active]
                going = [h for h in listed if h.config_hash == run.config_hash]
                # A stream's continuous run of another config still on the checkpoint (fluid
                # apply is stopping it between batches): a second writer must not start.
                other = [
                    h
                    for h in listed
                    if run.checkpoint_id
                    and h.config_hash != run.config_hash
                    and h.checkpoint_id == run.checkpoint_id
                ]
                if other and not going:
                    out.action, out.error, out.run_id = "refused", True, other[0].run_id
                    out.message = (
                        f"run {other[0].run_id} of another config is still going on the "
                        "checkpoint (fluid apply is stopping it): nothing was started; the "
                        "next scheduled run starts once it has stopped"
                    )
                    return out
                if going:
                    started.append(going[0].run_id)
                    out.action = "adopted"
                    say(
                        f"Spark {run.build}: run {started[0]} is already going on "
                        f"{out.where}; waiting for it"
                    )
                else:
                    started.append(adapter.start(_token(run)))
                    say(f"Spark {run.build}: started run {started[0]} on {out.where}")
                run_id = out.run_id = started[0]
                ended = clock.until(
                    lambda: adapter.get(run_id), lambda i: i.state in TERMINAL, timeout
                )
        except KeyboardInterrupt:
            # Ctrl-C, SIGTERM or SIGHUP (a killed Airflow task): the guard cancelled the run.
            out.action, out.error, out.state = "cancelled", True, CANCELLING
            out.run_id = started[0] if started else None
            if not started:
                out.message = "interrupted before a run started"
            elif out.cancelled:
                out.message = f"interrupted while waiting; run {started[0]} was cancelled"
            else:
                out.message = (
                    f"interrupted while waiting; run {started[0]} could not be cancelled "
                    "and may still be going: cancel it by hand"
                )
            return out
        out.state = ended.state
        if ended.state not in TERMINAL:
            cancel()
            out.action, out.error = "failed", True
            out.message = f"still {ended.raw_state} after {int(timeout)} s; " + (
                "it was cancelled"
                if out.cancelled
                else "it could not be cancelled and may still be going: cancel it by hand"
            )
        elif ended.state != SUCCEEDED:
            out.action, out.error, out.message = "failed", True, _failure_text(ended)
        else:
            out.action = "succeeded"
        return out
    except Exception as exc:  # noqa: BLE001 - the AWS error is the run's outcome
        failed = _error_outcome(run, exc)
        if started:  # reads failed: the run is left going, and a retry adopts it
            failed.run_id = started[0]
            failed.message += f"; run {started[0]} was left going (a retry waits for it)"
        return failed


def run_build(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    *,
    dry_run: bool,
    say: Callable[[str], None] = print,
    clients: Optional[ClientFactory] = None,
    local_host: Any = None,
    run_id: Optional[str] = None,
    stamp_source: Any = None,
) -> Optional[int]:
    """The build phase of ``fluid apply --mode amend-and-build`` for one Spark build: how its
    scheduler runs it. A local run lives in ``local_host`` (the contract's directory). The
    runner stamps ``run_id`` (the pipeline's) on each commit, and what ``stamp_source`` names.

    A scheduled build (batch, or a stream triggered ``availableNow``) gets one run, waited for
    until it ends or ``execution.runtime.timeout`` (:data:`DEFAULT_RUN_WAIT_S` when unset)
    passes: 0 when it SUCCEEDED, 1 otherwise. A dry run says what it would start and reads
    nothing from AWS. A stream ``fluid apply`` keeps running (its ``spark.reconcile`` action)
    has no run to start: ``None``.

    Raises:
        FluidUserError: the operator's policy refuses the build.
        ValueError: the build does not compile, or :func:`run_refusal` names why it cannot
            run; nothing was started.
        KeyboardInterrupt: Ctrl-C, SIGTERM or SIGHUP while the run was waited on; the run
            was cancelled, and the apply stops as an interrupt always stops it.
    """
    import copy

    from fluid_build._contract_loader import resolve_contract_env_templates
    from fluid_build.build_runners.spark._desired import desired_runs, stamps_from
    from fluid_build.spark.compiler import run_timeout_seconds

    bid = str(build.get("id"))
    refusal = run_refusal(contract, build)
    if refusal:
        raise ValueError(refusal)
    # As fluid plan and fluid apply read it: ``{{ env.* }}`` resolved.
    resolved = resolve_contract_env_templates(copy.deepcopy(dict(contract)))
    stamps = stamps_from(contract, run_id=run_id, source=stamp_source)
    runs = desired_runs(resolved, build_id=bid, stamps=stamps)
    if not runs:
        raise ValueError(
            f"Spark build {bid}: forge's Spark runner runs a build of engine: spark with "
            "properties.parameters.spark; this one is not, so nothing runs it"
        )
    run = runs[0]
    if not run.scheduled:
        return None
    timeout = run_timeout_seconds(build) or DEFAULT_RUN_WAIT_S
    if dry_run:
        say(
            f"Spark {run.build} ({run.runtime}): would start one run and wait up to "
            f"{int(timeout)} s for it to end"
        )
        return 0
    outcome = run_once(run, clients=clients, local_host=local_host, timeout=timeout, say=say)
    say(outcome.line())
    if outcome.action == "cancelled":
        raise KeyboardInterrupt  # the run was cancelled; stop the apply, as Ctrl-C does
    return 1 if outcome.error else 0


def run_refusal(contract: Mapping[str, Any], build: Mapping[str, Any]) -> Optional[str]:
    """Why :func:`run_build` would refuse ``build`` without starting anything, else ``None``:
    a contract before the managed lane (fluidVersion 0.7.7), or a run that ends on its own
    with an ``execution.runtime.timeout`` it cannot read. Reads no cloud, so ``fluid apply``
    checks it before ``tofu apply`` changes anything."""
    from fluid_build.spark.compiler import run_timeout_seconds, runs_to_completion
    from fluid_build.streaming.compiler import managed_lane_enabled

    bid = build.get("id")
    if not managed_lane_enabled(contract):
        return (
            f"Spark build {bid}: forge's Spark runner reads a contract of fluidVersion 0.7.7 "
            "or later; this one is not, so nothing runs it"
        )
    if runs_to_completion(build):
        try:
            run_timeout_seconds(build)
        except ValueError as exc:
            return f"Spark build {bid}: {exc}"
    return None
