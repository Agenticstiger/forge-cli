# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Cross-CLI run-id correlation.

The 11-stage pipeline issues separate ``fluid bundle``, ``fluid plan``,
``fluid apply``, ``fluid verify``, ``fluid publish`` invocations.
Without a shared run-id, OpenTelemetry spans emitted by each stage
land in unrelated traces — operators can't reconstruct "what
happened on Monday's deploy of orders_v1?" by ID.

This module provides a single canonical run-id resolver:

1. ``$FLUID_RUN_ID`` env var (operator override / CI injection).
2. The scheduler's run, for a command a scheduler started (:func:`scheduler_run`): an id
   derived from it, never persisted.
3. ``.fluid/run-id.txt`` (persisted between stages).
4. Newly-generated id (first stage of a run).

Every CLI stage that emits OTel spans should call
:func:`get_or_create_run_id` early and stamp the result onto its root
span via ``traced_span(name, {"fluid.run_id": run_id})``. The id is
short (12 chars) so it fits in span attributes without truncation.

For the staged copilot pipeline, the run_id flows from the staged
session into ``fluid.copilot.staged.invocation`` spans (see
``coordinator.py``). For non-AI commands (bundle / plan / apply),
the run_id is read from this module at command entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Mapping, NamedTuple, Optional

LOG = logging.getLogger("fluid.observability.run_id")

#: Canonical env var name. Set to override the persisted id.
RUN_ID_ENV_VAR = "FLUID_RUN_ID"

#: Persisted file location relative to ``.fluid/``. Cleaned up on
#: successful completion of the final pipeline stage (publish or
#: schedule-sync) — see ``cli/_cleanup.py``.
RUN_ID_FILE = "run-id.txt"

#: The scheduler's run a command belongs to, set by whatever started it: the job the scheduler
#: runs (Airflow's DAG id) and its run of that job (Airflow's run id, which is unique per DAG
#: only). The DAGs ``fluid generate schedule`` writes set them from Airflow's task context.
SCHEDULER_JOB_ENV = "FLUID_SCHEDULER_JOB"
SCHEDULER_RUN_ID_ENV = "FLUID_SCHEDULER_RUN_ID"


class SchedulerRun(NamedTuple):
    """A scheduler's run: the job it runs (None when not named) and the run's id."""

    job: Optional[str]
    run_id: str


def scheduler_run(environ: Mapping[str, str] = os.environ) -> Optional[SchedulerRun]:
    """The scheduler run this command belongs to, or None when no scheduler started it.

    Read from :data:`SCHEDULER_RUN_ID_ENV` with :data:`SCHEDULER_JOB_ENV`, else from the task
    context Airflow 2 and 3 export to every task (``AIRFLOW_CTX_DAG_RUN_ID`` with
    ``AIRFLOW_CTX_DAG_ID``; a BashOperator's command always has them). A pair comes whole from
    one of the two, so a run id is never paired with another source's job. A job with no run
    names no run.
    """
    for job, run in (
        (environ.get(SCHEDULER_JOB_ENV), environ.get(SCHEDULER_RUN_ID_ENV)),
        (environ.get("AIRFLOW_CTX_DAG_ID"), environ.get("AIRFLOW_CTX_DAG_RUN_ID")),
    ):
        run_id = (run or "").strip()
        if run_id:
            return SchedulerRun((job or "").strip() or None, run_id)
    return None


def _scheduled_run_id(run: SchedulerRun) -> str:
    """A run id of the usual shape that names ``run`` alone: the same for every command of one
    scheduler run, another for each other run. OpenLineage's Airflow integration names its runs
    the same way, from the DAG run rather than from anything kept on the worker."""
    text = json.dumps([run.job, run.run_id], ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:12]


def _generate_id() -> str:
    """Build a fresh run-id. 12 hex chars (48 bits) is plenty of entropy
    for a single workspace + short enough for OTel attribute tags."""
    return secrets.token_hex(6)


def _runtime_dir(workspace_root: Optional[Path] = None) -> Path:
    """Resolve ``<workspace>/.fluid``, creating it if missing."""
    root = workspace_root or Path.cwd()
    target = root / ".fluid"
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_or_create_run_id(
    workspace_root: Optional[Path] = None,
    *,
    create_persisted_file: bool = True,
) -> str:
    """Return the active run-id, creating one if none exists.

    Resolution order:

    1. ``$FLUID_RUN_ID`` (env var override).
    2. The run of the scheduler that started this command (:func:`scheduler_run`): an id
       derived from it, the same for each command of that run, neither read from nor written
       to ``.fluid/run-id.txt``. A worker's checkout keeps that file from run to run, so
       reading it would give every scheduled run one id.
    3. ``.fluid/run-id.txt`` (persisted from a prior stage).
    4. Newly generated id, persisted to ``.fluid/run-id.txt`` when
       ``create_persisted_file=True`` (the default).

    Args:
        workspace_root: Workspace directory containing ``.fluid``.
            Defaults to the current working directory.
        create_persisted_file: When True (default), a freshly
            generated id is persisted to ``.fluid/run-id.txt`` so
            subsequent CLI invocations in the same workspace can
            correlate. Set False to suppress the side effect (tests).

    Returns:
        12-char hex run-id, e.g. ``"a3f9b21c44d8"``.
    """
    env_value = os.environ.get(RUN_ID_ENV_VAR)
    if env_value:
        return env_value.strip()

    scheduled = scheduler_run()
    if scheduled is not None:
        return _scheduled_run_id(scheduled)

    persisted_path = _runtime_dir(workspace_root) / RUN_ID_FILE
    if persisted_path.is_file():
        try:
            existing = persisted_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError as exc:  # pragma: no cover — defensive
            LOG.debug("run_id_read_failed: path=%s error=%s", persisted_path, exc)

    run_id = _generate_id()
    if create_persisted_file:
        try:
            persisted_path.write_text(run_id + "\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            LOG.debug("run_id_write_failed: path=%s error=%s", persisted_path, exc)
    return run_id


def clear_run_id(workspace_root: Optional[Path] = None) -> None:
    """Delete the persisted run-id file.

    Called by the final pipeline stage (publish / schedule-sync) when
    the run completes successfully — keeps the workspace from
    accumulating stale ids that would silently cross-correlate
    across logically-distinct deploys.
    """
    persisted_path = _runtime_dir(workspace_root) / RUN_ID_FILE
    try:
        persisted_path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        LOG.debug("run_id_clear_failed: path=%s error=%s", persisted_path, exc)


def run_id_span_attribute(workspace_root: Optional[Path] = None) -> dict:
    """Convenience helper for OTel span emission.

    Returns ``{"fluid.run_id": <id>}`` ready to merge into a
    ``traced_span(name, {...})`` attributes dict. Operators query by
    this attribute in observability dashboards to group all spans
    from one logical pipeline run.
    """
    return {"fluid.run_id": get_or_create_run_id(workspace_root)}


__all__ = [
    "RUN_ID_ENV_VAR",
    "RUN_ID_FILE",
    "get_or_create_run_id",
    "SCHEDULER_JOB_ENV",
    "SCHEDULER_RUN_ID_ENV",
    "SchedulerRun",
    "scheduler_run",
    "clear_run_id",
    "run_id_span_attribute",
]
