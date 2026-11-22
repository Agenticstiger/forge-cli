# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Cross-CLI run-id correlation — pin the resolution order.

The 11-stage pipeline (bundle → plan → apply → verify → publish)
emits OTel spans from separate CLI invocations. Without a shared
run-id, dashboards can't group "Monday's deploy of orders_v1" by
ID. ``observability/run_id.py`` defines the resolution order:

1. ``$FLUID_RUN_ID`` env var (operator override / CI injection).
2. ``.fluid/run-id.txt`` (persisted between stages).
3. Newly-generated id (first stage of a run, persisted for next).

Pin every layer so a future contributor can't silently break the
correlation guarantee.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fluid_build.observability.run_id import (
    RUN_ID_ENV_VAR,
    RUN_ID_FILE,
    SCHEDULER_JOB_ENV,
    SCHEDULER_RUN_ID_ENV,
    SchedulerRun,
    clear_run_id,
    get_or_create_run_id,
    run_id_span_attribute,
    scheduler_run,
)

#: Every variable that names a scheduler's run: fluid's own, then Airflow's task context.
_SCHEDULER_NAMES = (
    SCHEDULER_JOB_ENV,
    SCHEDULER_RUN_ID_ENV,
    "AIRFLOW_CTX_DAG_ID",
    "AIRFLOW_CTX_DAG_RUN_ID",
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """Hermetic workspace with no env / persisted state."""
    monkeypatch.delenv(RUN_ID_ENV_VAR, raising=False)
    for name in _SCHEDULER_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestEnvOverride:
    def test_env_var_wins_over_persisted(self, workspace, monkeypatch):
        # Persist one id then set env to a different one.
        first = get_or_create_run_id(workspace)
        monkeypatch.setenv(RUN_ID_ENV_VAR, "ci-build-42")
        assert get_or_create_run_id(workspace) == "ci-build-42"
        # Persisted value still on disk untouched.
        persisted = (workspace / ".fluid" / RUN_ID_FILE).read_text().strip()
        assert persisted == first

    def test_env_var_strips_whitespace(self, workspace, monkeypatch):
        monkeypatch.setenv(RUN_ID_ENV_VAR, "  padded-id  \n")
        assert get_or_create_run_id(workspace) == "padded-id"

    def test_empty_env_var_falls_through(self, workspace, monkeypatch):
        # When env is set to empty string, fall through to persisted/new id.
        monkeypatch.setenv(RUN_ID_ENV_VAR, "")
        rid = get_or_create_run_id(workspace)
        # Generated id is 12 hex chars.
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


class TestPersisted:
    def test_first_call_generates_and_persists(self, workspace):
        rid = get_or_create_run_id(workspace)
        persisted = (workspace / ".fluid" / RUN_ID_FILE).read_text().strip()
        assert rid == persisted

    def test_second_call_returns_persisted(self, workspace):
        rid1 = get_or_create_run_id(workspace)
        rid2 = get_or_create_run_id(workspace)
        assert rid1 == rid2

    def test_create_persisted_file_false_skips_disk(self, workspace):
        rid = get_or_create_run_id(workspace, create_persisted_file=False)
        assert len(rid) == 12
        # ``.fluid/run-id.txt`` should NOT exist.
        assert not (workspace / ".fluid" / RUN_ID_FILE).exists()


class TestClear:
    def test_clear_removes_persisted_file(self, workspace):
        rid = get_or_create_run_id(workspace)
        path = workspace / ".fluid" / RUN_ID_FILE
        assert path.exists()
        clear_run_id(workspace)
        assert not path.exists()

    def test_clear_is_idempotent(self, workspace):
        # Calling clear before any id was created shouldn't raise.
        clear_run_id(workspace)
        clear_run_id(workspace)


class TestSpanAttribute:
    def test_returns_canonical_dict(self, workspace):
        attr = run_id_span_attribute(workspace)
        assert "fluid.run_id" in attr
        assert len(attr["fluid.run_id"]) == 12

    def test_env_override_flows_through(self, workspace, monkeypatch):
        monkeypatch.setenv(RUN_ID_ENV_VAR, "ci-id-99")
        attr = run_id_span_attribute(workspace)
        assert attr == {"fluid.run_id": "ci-id-99"}


def _scheduled(monkeypatch, job, run, *, airflow: bool = False) -> None:
    names = ("AIRFLOW_CTX_DAG_ID", "AIRFLOW_CTX_DAG_RUN_ID") if airflow else _SCHEDULER_NAMES[:2]
    for name, value in zip(names, (job, run), strict=True):
        monkeypatch.setenv(name, value)


class TestUnderAScheduler:
    """A command a scheduler started (an Airflow task) is that scheduler run's: a worker's
    checkout keeps ``.fluid/run-id.txt`` from run to run, so reading it would give every
    scheduled run of every day one id."""

    def test_a_scheduled_run_has_an_id_of_its_own(self, workspace, monkeypatch):
        _scheduled(monkeypatch, "orders__load", "scheduled__2026-11-21T00:00:00+00:00")
        first = get_or_create_run_id(workspace)
        assert len(first) == 12 and all(c in "0123456789abcdef" for c in first)
        assert get_or_create_run_id(workspace) == first  # every command of the run shares it
        _scheduled(monkeypatch, "orders__load", "scheduled__2026-11-22T00:00:00+00:00")
        assert get_or_create_run_id(workspace) != first
        _scheduled(monkeypatch, "clicks__load", "scheduled__2026-11-21T00:00:00+00:00")
        assert get_or_create_run_id(workspace) != first  # a run id is unique per DAG only

    def test_the_persisted_id_is_neither_read_nor_written(self, workspace, monkeypatch):
        persisted = get_or_create_run_id(workspace)
        path = workspace / ".fluid" / RUN_ID_FILE
        _scheduled(monkeypatch, "orders__load", "manual__2026-11-21T10:15:30+00:00")
        assert get_or_create_run_id(workspace) != persisted
        assert path.read_text(encoding="utf-8").strip() == persisted
        path.unlink()
        get_or_create_run_id(workspace)
        assert not path.exists()

    def test_fluid_run_id_still_wins(self, workspace, monkeypatch):
        _scheduled(monkeypatch, "orders__load", "scheduled__2026-11-21T00:00:00+00:00")
        monkeypatch.setenv(RUN_ID_ENV_VAR, "ci-build-42")
        assert get_or_create_run_id(workspace) == "ci-build-42"

    def test_airflows_own_task_context_names_the_run(self, workspace, monkeypatch):
        _scheduled(monkeypatch, "orders__load", "scheduled__2026-11-21T00:00:00+00:00")
        named_by_fluid = get_or_create_run_id(workspace)
        for name in _SCHEDULER_NAMES[:2]:
            monkeypatch.delenv(name)
        _scheduled(
            monkeypatch, "orders__load", "scheduled__2026-11-21T00:00:00+00:00", airflow=True
        )
        assert get_or_create_run_id(workspace) == named_by_fluid

    def test_a_job_with_no_run_is_no_scheduler_run(self, workspace, monkeypatch):
        monkeypatch.setenv(SCHEDULER_JOB_ENV, "orders__load")
        monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", "orders__load")
        assert scheduler_run() is None
        assert get_or_create_run_id(workspace) == get_or_create_run_id(workspace)
        assert (workspace / ".fluid" / RUN_ID_FILE).exists()


@pytest.mark.parametrize(
    "environ, expected",
    [
        ({}, None),
        ({SCHEDULER_RUN_ID_ENV: "  "}, None),
        ({SCHEDULER_RUN_ID_ENV: " r1 ", SCHEDULER_JOB_ENV: " dag "}, SchedulerRun("dag", "r1")),
        ({SCHEDULER_RUN_ID_ENV: "r1"}, SchedulerRun(None, "r1")),
        ({"AIRFLOW_CTX_DAG_RUN_ID": "r2", "AIRFLOW_CTX_DAG_ID": "dag"}, SchedulerRun("dag", "r2")),
        # A pair is taken whole from one source: fluid's run id never takes Airflow's DAG id.
        (
            {
                SCHEDULER_RUN_ID_ENV: "r1",
                "AIRFLOW_CTX_DAG_ID": "d2",
                "AIRFLOW_CTX_DAG_RUN_ID": "r2",
            },
            SchedulerRun(None, "r1"),
        ),
    ],
)
def test_the_scheduler_run_comes_whole_from_fluids_names_else_airflows(environ, expected):
    assert scheduler_run(environ) == expected
