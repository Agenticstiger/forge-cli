# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Keep each streaming Spark build's job running with the config the contract declares.

After ``tofu apply`` provisions a Spark build (``fluid apply``), :func:`reconcile` makes its
run match the contract, idempotently:

1. Find the build's runs (:mod:`._adapters`), each marked with the config hash it started
   with.
2. A running job with this hash: nothing to do.
3. A running job with another hash: cancel it, wait until it has stopped, and start the new
   one on the SAME checkpoint, so it resumes where the old one stopped. Spark's checkpoint
   holds the offsets of one topic on one cluster, so a new source on the old checkpoint is
   refused before anything is cancelled; moving ``properties.stream.checkpoint`` to a new
   prefix is how a stream changes source.
4. No running job: start one, unless the last run with this hash failed. On EMR Serverless a
   streaming job retries by itself until it fails more often than ``maxFailedAttemptsPerHour``
   in an hour, and then stops: starting it again would fail the same way, so that run is an
   error, with its id and reason, until the build changes or the operator restarts it
   (``fluid spark run --restart``).

A scheduled build (batch, or a stream with ``trigger.availableNow``) is never reconciled:
its scheduler starts it with :func:`run_once` (``fluid spark run``), which waits for the run
to end and cancels it on Ctrl-C, SIGTERM or SIGHUP, as ``_databricks_job_run`` does.

Polling follows the databricks-sdk run waiter already adapted in ``_databricks_job_run``: a
poll a second longer each time, at most :data:`MAX_POLL_S`.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from fluid_build.build_runners.spark._adapters import (
    CANCELLING,
    FAILED,
    PENDING,
    SUCCEEDED,
    TERMINAL,
    Adapter,
    ClientFactory,
    RunInfo,
    SparkRunError,
    adapter_for,
)
from fluid_build.build_runners.spark._desired import DesiredRun

MAX_POLL_S = 15.0
#: How long a reconcile waits for a cancelled run to stop, and for a started one to leave
#: its pending states; ``FLUID_SPARK_WAIT_SECONDS`` overrides both.
DEFAULT_WAIT_S = 900
#: How long ``fluid spark run`` waits for a scheduled run to end (``--timeout`` overrides).
DEFAULT_RUN_WAIT_S = 6 * 3600
WAIT_ENV = "FLUID_SPARK_WAIT_SECONDS"


@dataclass
class Outcome:
    """What happened to one build. ``error`` outcomes make ``fluid apply`` fail."""

    build: str
    runtime: str
    action: str  # unchanged | started | restarted | scheduled | refused | failed | planned
    where: str = ""
    run_id: Optional[str] = None
    state: Optional[str] = None
    message: str = ""
    error: bool = False
    cancelled: List[str] = field(default_factory=list)

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
        }


def _clean(text: Any) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    return redact_secret_text(" ".join(str(text or "").split()))[:400]


def wait_budget(env: Optional[Mapping[str, str]] = None) -> float:
    raw = (os.environ if env is None else env).get(WAIT_ENV)
    try:
        value = float(raw) if raw else DEFAULT_WAIT_S
    except ValueError:
        value = DEFAULT_WAIT_S
    return value if value > 0 else DEFAULT_WAIT_S


class _Clock:
    def __init__(self, sleep: Callable[[float], None], now: Callable[[], float]) -> None:
        self.sleep, self.now = sleep, now

    def until(
        self, read: Callable[[], RunInfo], done: Callable[[RunInfo], bool], budget: float
    ) -> RunInfo:
        """Poll ``read`` until ``done`` or the budget is spent; the last read either way."""
        deadline = self.now() + budget
        delay = 1.0
        info = read()
        while not done(info) and self.now() < deadline:
            self.sleep(min(delay, max(0.0, deadline - self.now())))
            delay = min(delay + 1.0, MAX_POLL_S)
            info = read()
        return info


def _token(run: DesiredRun) -> str:
    """A start's idempotency token (EMR Serverless, EMR on EKS: 1-64 ``[A-Za-z0-9._-]``)."""
    return f"fluid-{run.config_hash[:16]}-{uuid.uuid4().hex[:20]}"


def _failure_text(info: RunInfo) -> str:
    attempts = f" after {info.attempt} attempts" if info.attempt and info.attempt > 1 else ""
    reason = f": {_clean(info.details)}" if info.details else ""
    return f"run {info.run_id} ended {info.raw_state or info.state}{attempts}{reason}"


def _refuse_new_source(run: DesiredRun, last: Optional[RunInfo]) -> Optional[str]:
    if not (last and run.checkpoint_id and run.source_id):
        return None
    if last.checkpoint_id != run.checkpoint_id or not last.source_id:
        return None
    if last.source_id == run.source_id:
        return None
    return (
        f"the stream changed (topic {run.topic} or its cluster), but checkpoint "
        f"{run.checkpoint} holds the offsets of the stream run {last.run_id} read. Nothing "
        "was cancelled. To read the new stream, set properties.stream.checkpoint to a new "
        "prefix: the job then starts from properties.stream.startingOffsets"
    )


def _reconcile_one(
    run: DesiredRun,
    adapter: Adapter,
    *,
    clock: _Clock,
    budget: float,
    dry_run: bool,
    restart_failed: bool,
) -> Outcome:
    out = Outcome(run.build, run.runtime, "unchanged", where=adapter.where())
    history = adapter.runs()
    active = [h for h in history if h.active]
    last = active[0] if active else (history[0] if history else None)
    refusal = _refuse_new_source(run, last)
    if refusal:
        out.action, out.error, out.message = "refused", True, refusal
        return out
    matching = [a for a in active if a.config_hash == run.config_hash and a.state != CANCELLING]
    stale = [a for a in active if a not in matching]
    if matching and not stale:
        current = matching[0]
        out.run_id, out.state = current.run_id, current.state
        if current.attempt and current.attempt > 1:
            out.message = f"attempt {current.attempt}: the job has restarted after failures"
        return out
    if (
        not active
        and last is not None
        and last.config_hash == run.config_hash
        and last.state == FAILED
        and not restart_failed
    ):
        out.action, out.error, out.run_id, out.state = "failed", True, last.run_id, last.state
        out.message = (
            f"{_failure_text(last)}. It is not started again with the same config, which "
            "would fail the same way: fix the cause, then run fluid spark run "
            f"<contract> --build-id {run.build} --restart, or change the build"
        )
        return out
    if dry_run:
        out.action = "planned"
        out.message = (
            f"would cancel {', '.join(s.run_id for s in stale)} and start the new config"
            if stale
            else "would start the job"
        )
        return out
    for old in stale:
        if old.state != CANCELLING:
            adapter.cancel(old.run_id)
        out.cancelled.append(old.run_id)
    for old_id in out.cancelled:
        ended = clock.until(lambda i=old_id: adapter.get(i), lambda i: i.state in TERMINAL, budget)
        if ended.state not in TERMINAL:
            out.action, out.error, out.run_id, out.state = "failed", True, old_id, ended.state
            out.message = (
                f"run {old_id} was cancelled but is still {ended.raw_state} after "
                f"{int(budget)} s ({WAIT_ENV}); nothing new was started, and fluid apply "
                "starts it once the old run has stopped"
            )
            return out
    if matching:  # the new config already runs; the stale runs were extra
        out.run_id, out.state = matching[0].run_id, matching[0].state
        return out
    run_id = adapter.start(_token(run))
    started = clock.until(lambda: adapter.get(run_id), lambda i: i.state != PENDING, budget)
    out.action = "restarted" if out.cancelled else "started"
    out.run_id, out.state = run_id, started.state
    if started.state in TERMINAL:
        # A long-running job that ends at once failed, whatever it reports.
        out.error = True
        out.message = _failure_text(started)
    elif started.state == PENDING:
        out.message = f"still starting after {int(budget)} s; fluid spark status follows it"
    return out


def reconcile(
    runs: Sequence[DesiredRun],
    *,
    clients: Optional[ClientFactory] = None,
    dry_run: bool = False,
    restart_failed: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    env: Optional[Mapping[str, str]] = None,
) -> List[Outcome]:
    """Reconcile every long-running build in ``runs``; scheduled builds are reported only.

    An AWS error on one build is that build's error outcome; the others still reconcile.
    """
    clock, budget = _Clock(sleep, now), wait_budget(env)
    outcomes = []
    for run in runs:
        if run.scheduled:
            outcomes.append(
                Outcome(
                    run.build,
                    run.runtime,
                    "scheduled",
                    message=f"a scheduler starts it: fluid spark run <contract> --build-id "
                    f"{run.build}",
                )
            )
            continue
        try:
            adapter = adapter_for(run, clients)
            outcomes.append(
                _reconcile_one(
                    run,
                    adapter,
                    clock=clock,
                    budget=budget,
                    dry_run=dry_run,
                    restart_failed=restart_failed,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one build's AWS error is its outcome
            outcomes.append(_error_outcome(run, exc))
    return outcomes


def _error_outcome(run: DesiredRun, exc: BaseException) -> Outcome:
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        code = str((response.get("Error") or {}).get("Code") or "")
    text = _clean(exc) if isinstance(exc, SparkRunError) else f"{type(exc).__name__}: {_clean(exc)}"
    return Outcome(
        run.build,
        run.runtime,
        "failed",
        message=f"{code + ': ' if code and code not in text else ''}{text}",
        error=True,
    )


def run_once(
    run: DesiredRun,
    *,
    clients: Optional[ClientFactory] = None,
    wait: bool = True,
    timeout: float = DEFAULT_RUN_WAIT_S,
    restart: bool = False,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    env: Optional[Mapping[str, str]] = None,
) -> Outcome:
    """``fluid spark run``: one run of a build, for a scheduler.

    A scheduled build starts a run and, with ``wait``, waits for it to end; the outcome is an
    error unless it SUCCEEDED. Ctrl-C, SIGTERM or SIGHUP while it is waited on cancel it, and
    the outcome is ``cancelled`` (an error), so ``--json`` still prints its one line.
    A long-running build is reconciled instead (``restart`` starts it again after a failure).
    """
    from fluid_build.build_runners._databricks_job_run import interrupt_guard

    if not run.scheduled:
        return reconcile(
            [run], clients=clients, restart_failed=restart, sleep=sleep, now=now, env=env
        )[0]
    try:
        adapter = adapter_for(run, clients)
        out = Outcome(run.build, run.runtime, "started", where=adapter.where())
        run_id = adapter.start(_token(run))
        out.run_id = run_id
        say(f"Spark {run.build}: started run {run_id} on {out.where}")
        if not wait:
            out.state = adapter.get(run_id).state
            return out

        def cancel() -> None:
            try:
                adapter.cancel(run_id)
                say(f"Spark {run.build}: cancelled run {run_id}")
            except Exception as exc:  # noqa: BLE001 - say it; the interrupt goes on
                say(f"Spark {run.build}: could not cancel run {run_id}: {_clean(exc)}")

        clock = _Clock(sleep, now)
        try:
            with interrupt_guard(cancel):
                ended = clock.until(
                    lambda: adapter.get(run_id), lambda i: i.state in TERMINAL, timeout
                )
        except KeyboardInterrupt:
            # Ctrl-C, SIGTERM or SIGHUP (a killed Airflow task): the guard cancelled the run.
            out.action, out.error, out.state = "cancelled", True, CANCELLING
            out.message = f"interrupted while waiting; run {run_id} was cancelled"
            return out
        out.state = ended.state
        if ended.state not in TERMINAL:
            cancel()
            out.action, out.error = "failed", True
            out.message = f"still {ended.raw_state} after {int(timeout)} s; it was cancelled"
        elif ended.state != SUCCEEDED:
            out.action, out.error, out.message = "failed", True, _failure_text(ended)
        else:
            out.action = "succeeded"
        return out
    except Exception as exc:  # noqa: BLE001 - the AWS error is the run's outcome
        return _error_outcome(run, exc)


def status(
    runs: Sequence[DesiredRun], *, clients: Optional[ClientFactory] = None, limit: int = 5
) -> List[Dict[str, Any]]:
    """Each build's recent runs, newest first, and whether the newest runs the contract's config."""
    rows = []
    for run in runs:
        row: Dict[str, Any] = {
            "build": run.build,
            "runtime": run.runtime,
            "configHash": run.config_hash,
        }
        try:
            adapter = adapter_for(run, clients)
            row["where"] = adapter.where()
            history = adapter.runs()[:limit]
            row["runs"] = [
                {
                    **h.as_dict(),
                    "details": _clean(h.details),
                    "current": h.config_hash == run.config_hash,
                }
                for h in history
            ]
        except Exception as exc:  # noqa: BLE001 - one build's error is its row
            row["error"] = _error_outcome(run, exc).message
        rows.append(row)
    return rows
