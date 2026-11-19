# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Stop the runs a destroy would leave going: a Spark build's, before its role and artifacts.

``fluid apply`` reconciles the builds the contract has (:mod:`._reconcile`). A build it no
longer has is found where it ran (:func:`._desired.located_run`), and every active run of it
is cancelled and waited for until it has ended (:func:`stop_runs`). An EMR Serverless
application a destroy deletes has every active run in it cancelled, then is stopped and
waited for (:func:`stop_application`): EMR Serverless deletes only a stopped or created
application, and one that ran jobs stops by itself only after its idle timeout. Apache
Airflow's ``EmrServerlessHook.cancel_running_jobs`` and
``EmrServerlessStopApplicationOperator(force_stop=True)`` (Apache-2.0) do the same before a
stop. A target that no longer exists holds no run.
"""

from __future__ import annotations

import time
from functools import partial
from typing import Callable, List, Mapping, Optional, Sequence

from fluid_build.build_runners.spark._adapters import (
    CANCELLING,
    TERMINAL,
    Adapter,
    ClientFactory,
    EmrServerless,
    RunInfo,
    adapter_for,
)
from fluid_build.build_runners.spark._desired import DesiredRun
from fluid_build.build_runners.spark._reconcile import (
    WAIT_ENV,
    Outcome,
    _Clock,
    _error_outcome,
    wait_budget,
)

#: The states EMR Serverless deletes an application in, and the one it is gone in.
APPLICATION_IDLE = frozenset({"CREATED", "STOPPED", "TERMINATED"})
#: The error codes of a target that no longer exists (EMR Serverless, EMR on EKS, Glue).
_GONE = frozenset({"ResourceNotFoundException", "EntityNotFoundException"})


def gone(exc: BaseException) -> bool:
    """``exc`` says the run's target no longer exists, so nothing runs on it."""
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, Mapping) else None
    if not isinstance(error, Mapping):
        return False
    code = str(error.get("Code") or "")
    # EMR answers a cluster id it no longer knows with "Cluster id '...' is not valid."
    return code in _GONE or (
        code == "InvalidRequestException" and "is not valid" in str(error.get("Message") or "")
    )


def _ended(info: RunInfo) -> bool:
    return info.state in TERMINAL


def _cancel_all(
    adapter: Adapter, active: Sequence[RunInfo], out: Outcome, clock: _Clock, budget: float
) -> None:
    """Cancel ``active`` (one already cancelling is only waited for), then wait for each to
    end; ``out`` is the error when one is still going after ``budget``. A run that ended by
    itself after it was listed refuses the cancel, and is left out."""
    for info in active:
        if info.state != CANCELLING:
            try:
                adapter.cancel(info.run_id)
            except Exception:
                if adapter.get(info.run_id).state not in TERMINAL:
                    raise
                continue
        out.cancelled.append(info.run_id)
    for run_id in out.cancelled:
        ended = clock.until(partial(adapter.get, run_id), _ended, budget)
        if ended.state not in TERMINAL:
            out.action, out.error, out.run_id, out.state = "failed", True, run_id, ended.state
            out.message = (
                f"run {run_id} was cancelled but is still {ended.raw_state} after "
                f"{int(budget)} s ({WAIT_ENV})"
            )
            return
    if out.cancelled:
        they = "it runs" if len(out.cancelled) == 1 else "they run"
        out.message = (
            f"cancelled {', '.join(out.cancelled)} before the apply destroys what {they} with"
        )


def stop_runs(
    runs: Sequence[DesiredRun],
    *,
    clients: Optional[ClientFactory] = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    env: Optional[Mapping[str, str]] = None,
) -> List[Outcome]:
    """Cancel every active run of each of ``runs`` (:func:`._desired.located_run`) and wait
    until it has ended. ``dry_run`` reads the runs and cancels none. An AWS error is that
    build's error outcome; the others are still stopped."""
    clock, budget = _Clock(sleep, now), wait_budget(env)
    outcomes = []
    for run in runs:
        out = Outcome(run.build, run.runtime, "stopped")
        try:
            adapter = adapter_for(run, clients)
            out.where = adapter.where()
            active = [h for h in adapter.runs() if h.active]
            if dry_run:
                if active:
                    out.action = "planned"
                    out.message = f"would cancel {', '.join(h.run_id for h in active)}"
            else:
                _cancel_all(adapter, active, out, clock, budget)
        except Exception as exc:  # noqa: BLE001 - one build's AWS error is its outcome
            if not gone(exc):
                out = _error_outcome(run, exc)
        outcomes.append(out)
    return outcomes


def stop_application(
    run: DesiredRun,
    *,
    clients: Optional[ClientFactory] = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    env: Optional[Mapping[str, str]] = None,
) -> Outcome:
    """Every active run in ``run``'s EMR Serverless application (``target`` its
    ``applicationId``) cancelled and waited for, then the application stopped and waited
    for, so the destroy can delete it. ``dry_run`` reads and changes nothing."""
    clock, budget = _Clock(sleep, now), wait_budget(env)
    out = Outcome(run.build, run.runtime, "stopped")
    try:
        adapter = adapter_for(run, clients)
        if not isinstance(adapter, EmrServerless):
            raise TypeError(f"{run.runtime} has no application to stop")
        out.where = adapter.where()
        active = adapter.application_runs()
        state = adapter.application_state()
        if dry_run:
            if active or state not in APPLICATION_IDLE:
                out.action = "planned"
                runs = f"cancel {', '.join(h.run_id for h in active)} and " if active else ""
                out.message = f"would {runs}stop the application ({state})"
            return out
        _cancel_all(adapter, active, out, clock, budget)
        if out.error:
            return out
        if active:  # read again: the wait was long enough for the application to move
            state = adapter.application_state()
        if state in APPLICATION_IDLE:
            return out
        out.state = _stopped(adapter, state, clock, budget)
        if out.state not in APPLICATION_IDLE:
            out.action, out.error = "failed", True
            out.message = (
                f"the application is still {out.state} after {int(budget)} s ({WAIT_ENV}), "
                "and EMR Serverless deletes only a stopped or created one"
            )
        else:
            out.message = (out.message + "; " if out.message else "") + "stopped the application"
    except Exception as exc:  # noqa: BLE001 - an AWS error is the outcome
        if not gone(exc):
            return _error_outcome(run, exc)
    return out


def _stopped(adapter: EmrServerless, state: str, clock: _Clock, budget: float) -> str:
    """Stop the application from ``state`` and wait until it has stopped: its last state."""

    def read() -> RunInfo:
        # The application's state, read as a run's so the run waiter polls it.
        raw = adapter.application_state()
        return RunInfo(adapter.application_id(), raw, raw)

    if state == "STARTING":  # stopped once it has started
        state = clock.until(read, lambda i: i.raw_state != "STARTING", budget).raw_state
    if state == "STARTED":
        adapter.stop_application()
    elif state != "STOPPING":
        return state
    return clock.until(read, lambda i: i.raw_state in APPLICATION_IDLE, budget).raw_state
