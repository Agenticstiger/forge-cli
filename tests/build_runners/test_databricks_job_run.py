# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply --mode amend-and-build`` starts a Databricks dbt job now and waits for it.

Driven through ``build_runners/_databricks_job_run.py`` with a scripted ``api_client``
(the shape ``databricks_enforce.default_client`` returns) and a fake clock: no network,
no SDK. Pinned: the run starts with an idempotency token and queueing on, and nothing
but ``run-now`` is ever written to the job; its page URL and every state it enters are
printed, a queue reason once; a run that does not succeed fails the build with the task's
error and the end of its output, credentials masked and escape sequences gone; the wait
is capped, and the cap or Ctrl-C cancels the run; ``replace-and-build`` adds
``--full-refresh`` to the job's own ``dbt build``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import pytest

from fluid_build.build_runners import _databricks_job_run as job_run
from fluid_build.build_runners._databricks_job_run import (
    DEFAULT_TIMEOUT_SECONDS,
    TIMEOUT_ENV,
    JobRef,
    full_refresh_commands,
    jobs_in_state,
    run_job_build,
    started_builds,
    wait_budget,
)

pytestmark = pytest.mark.unit

URL = "https://dbc-1.cloud.databricks.com/?o=1#job/5/run/77"
TOKEN = "test-only-token-0f3c9a7e5b1d2468ace"  # pragma: allowlist secret
SETTINGS: Dict[str, Any] = {
    "name": "fluid sales.orders transform",
    "tags": {"fluid_product": "sales.orders", "fluid_build": "transform"},
    "tasks": [
        {
            "task_key": "dbt",
            "dbt_task": {"commands": ["dbt deps", "dbt build --select orders"]},
        }
    ],
}


def _run(
    state: str,
    *,
    code: Optional[str] = None,
    message: str = "",
    queue: Optional[Dict[str, str]] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    status: Dict[str, Any] = {"state": state}
    if queue:
        status["queue_details"] = queue
    if code:
        status["termination_details"] = {"code": code, "message": message}
    run: Dict[str, Any] = {"run_id": 77, "run_page_url": URL, "status": status}
    if tasks is not None:
        run["tasks"] = tasks
    return run


def _task(code: str, run_id: int = 78, attempt: int = 0) -> Dict[str, Any]:
    return {
        "task_key": "dbt",
        "run_id": run_id,
        "attempt_number": attempt,
        "status": {"state": "TERMINATED", "termination_details": {"code": code}},
    }


class _Api:
    """``WorkspaceClient.api_client``: answers each jobs endpoint; ``runs`` in turn."""

    def __init__(
        self,
        runs: List[Dict[str, Any]],
        *,
        settings: Optional[Mapping[str, Any]] = None,
        outputs: Optional[Dict[int, Dict[str, Any]]] = None,
        fail: Optional[Dict[str, BaseException]] = None,
        active: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.runs = list(runs)
        self.active = active or []
        self.settings = SETTINGS if settings is None else settings
        self.outputs = outputs or {}
        self.fail = fail or {}
        self.calls: List[tuple] = []

    def do(self, method: str, path: str, query: Any = None, body: Any = None, **_: Any) -> Any:
        endpoint = path.split("/api/2.2/jobs/", 1)[1]
        self.calls.append((method, endpoint, query, body))
        if endpoint in self.fail:
            raise self.fail[endpoint]
        if endpoint == "get":
            return {"job_id": query["job_id"], "settings": self.settings}
        if endpoint == "run-now":
            return {"run_id": 77, "number_in_job": 77}
        if endpoint == "runs/get":
            return self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
        if endpoint == "runs/get-output":
            return self.outputs.get(query["run_id"], {})
        if endpoint == "runs/cancel":
            return {}
        if endpoint == "runs/list":
            return {"runs": self.active}
        raise AssertionError(f"unexpected endpoint {method} {path}")

    def writes(self) -> List[str]:
        return [endpoint for method, endpoint, _q, _b in self.calls if method != "GET"]

    def body(self, endpoint: str) -> Dict[str, Any]:
        [body] = [b for _m, e, _q, b in self.calls if e == endpoint]
        return body


class _Client:
    def __init__(self, api: _Api) -> None:
        self.api_client = api


class _Time:
    """A clock that moves only when the code under test sleeps."""

    def __init__(self, interrupt_after: Optional[int] = None) -> None:
        self.now = 1000.0
        self.slept: List[float] = []
        self.interrupt_after = interrupt_after

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if self.interrupt_after is not None and len(self.slept) >= self.interrupt_after:
            raise KeyboardInterrupt
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (TIMEOUT_ENV, "DATABRICKS_TOKEN", "DATABRICKS_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


CONTRACT = {"id": "sales.orders", "builds": []}
BUILD = {"id": "transform", "engine": "dbt"}
JOBS = {"transform": [JobRef(5, "https://dbc-1.cloud.databricks.com/#job/5")]}


def _go(
    api: _Api,
    capsys: pytest.CaptureFixture,
    *,
    mode: str = "amend-and-build",
    time_: Optional[_Time] = None,
    env: Optional[Dict[str, str]] = None,
) -> tuple:
    time_ = time_ or _Time()
    rc = run_job_build(
        CONTRACT,
        BUILD,
        JOBS,
        mode=mode,
        client_factory=lambda: _Client(api),
        env=env or {},
        sleep=time_.sleep,
        clock=time_.clock,
    )
    captured = capsys.readouterr()
    return rc, " ".join((captured.out + captured.err).split())


# ── the run ─────────────────────────────────────────────────────────────────
class TestStartAndWait:
    def test_a_run_starts_now_and_its_success_is_reported(self, capsys) -> None:
        api = _Api([_run("PENDING"), _run("RUNNING"), _run("TERMINATED", code="SUCCESS")])
        rc, out = _go(api, capsys)

        assert rc == 0, out
        body = api.body("run-now")
        assert body["job_id"] == 5 and body["queue"] == {"enabled": True}
        assert len(body["idempotency_token"]) == 32 and "dbt_commands" not in body
        assert "started run 77 of Databricks job 'fluid sales.orders transform' (5)" in out
        assert f"run page: {URL}" in out
        assert "0:00 PENDING" in out and "RUNNING" in out and "TERMINATED SUCCESS" in out
        assert "Databricks run 77 succeeded in" in out

    def test_nothing_but_the_run_is_ever_written(self, capsys) -> None:
        # The job's settings, schedule and pause state are not touched: a scheduled
        # job keeps its schedule, and a job created paused stays paused.
        api = _Api([_run("TERMINATED", code="SUCCESS")])
        assert _go(api, capsys)[0] == 0
        assert api.writes() == ["run-now"]

    def test_a_queued_run_says_why_once_then_a_line_a_minute(self, capsys) -> None:
        reason = {
            "code": "ACTIVE_RUN_JOB_TASKS_LIMIT_REACHED",
            "message": "The run is queued: the workspace runs its limit of 5 concurrent tasks.",
        }
        queued = [_run("QUEUED", queue=reason) for _ in range(14)]
        api = _Api([*queued, _run("RUNNING"), _run("TERMINATED", code="SUCCESS")])
        rc, out = _go(api, capsys)

        assert rc == 0, out
        assert out.count("limit of 5 concurrent tasks") == 1
        assert "QUEUED - The run is queued" in out and "(ACTIVE_RUN_JOB_TASKS_LIMIT_REACHED)" in out
        assert "still QUEUED" in out
        assert [e for _m, e, _q, _b in api.calls if e == "runs/list"] == []

    @pytest.mark.parametrize(
        "active, said",
        [
            pytest.param(
                [{"run_id": 77}, {"run_id": 70}, {"run_id": 12}],
                "QUEUED - behind run 12 of this job, which runs one at a time",
                id="behind-another-run",
            ),
            pytest.param(
                [{"run_id": 77}],
                "QUEUED - waiting for the workspace's limit on runs or tasks at once "
                "(Databricks Free Edition runs at most 5 job tasks at a time)",
                id="workspace-limit",
            ),
        ],
    )
    def test_a_queued_run_without_a_reason_is_given_one(self, capsys, active, said) -> None:
        # What the live API answers for a queued run: a state, and no reason.
        queued = [_run("QUEUED") for _ in range(5)]
        api = _Api([*queued, _run("RUNNING"), _run("TERMINATED", code="SUCCESS")], active=active)
        rc, out = _go(api, capsys)
        assert rc == 0, out
        assert out.count(said) == 1
        listed = [q for _m, e, q, _b in api.calls if e == "runs/list"]
        assert listed == [{"job_id": 5, "active_only": "true", "limit": 25}]

    def test_runs_that_cannot_be_listed_leave_the_queued_line_plain(self, capsys) -> None:
        api = _Api(
            [_run("QUEUED"), _run("TERMINATED", code="SUCCESS")],
            fail={"runs/list": ConnectionError("reset")},
        )
        rc, out = _go(api, capsys)
        assert rc == 0 and "0:00 QUEUED 0:01" in out

    def test_the_poll_interval_grows_to_ten_seconds(self, capsys) -> None:
        time_ = _Time()
        api = _Api([*[_run("RUNNING") for _ in range(14)], _run("TERMINATED", code="SUCCESS")])
        assert _go(api, capsys, time_=time_)[0] == 0
        assert time_.slept[:3] == [1, 2, 3] and max(time_.slept) == 10

    def test_a_failed_run_fails_with_the_task_error_and_the_end_of_its_output(self, capsys) -> None:
        logs = "\n".join([f"noise {i}" for i in range(40)] + ["Database Error in model orders"])
        api = _Api(
            [
                _run(
                    "TERMINATED",
                    code="RUN_EXECUTION_ERROR",
                    message="Task dbt failed with message: dbt command failed.",
                    tasks=[_task("RUN_EXECUTION_ERROR")],
                )
            ],
            outputs={78: {"error": "dbt command failed", "logs": logs}},
        )
        rc, out = _go(api, capsys)

        assert rc == 1
        assert "Databricks run 77 ended RUN_EXECUTION_ERROR after" in out
        assert "Task dbt failed with message: dbt command failed." in out
        assert "task 'dbt': dbt command failed" in out
        assert "dbt last 30 of 41 lines:" in out
        assert "Database Error in model orders" in out
        assert "noise 10 " not in out and "noise 11 " in out
        assert out.count(f"run page: {URL}") == 2  # first, and again after the failure

    def test_only_the_latest_attempt_of_a_failed_task_is_read(self, capsys) -> None:
        tasks = [_task("RUN_EXECUTION_ERROR", 78, 0), _task("RUN_EXECUTION_ERROR", 79, 1)]
        api = _Api(
            [_run("TERMINATED", code="RUN_EXECUTION_ERROR", tasks=tasks)],
            outputs={79: {"error": "second attempt failed"}},
        )
        rc, out = _go(api, capsys)
        assert rc == 1 and "second attempt failed" in out
        read = [q["run_id"] for _m, e, q, _b in api.calls if e == "runs/get-output"]
        assert read == [79]

    def test_credentials_and_escape_sequences_never_reach_the_terminal(
        self, capsys, monkeypatch
    ) -> None:
        monkeypatch.setenv("DATABRICKS_TOKEN", TOKEN)
        api = _Api(
            [_run("TERMINATED", code="FAILED", tasks=[_task("FAILED")])],
            outputs={
                78: {
                    "error": f"bad token {TOKEN}",
                    "logs": f"\x1b[31mred line\x1b[0m\x07\nuses {TOKEN} here\r\n\x9b2Jdone",
                }
            },
        )
        rc, out = _go(api, capsys)
        assert rc == 1
        assert TOKEN not in out
        assert "red line" in out and "done" in out
        assert not any(ch in out for ch in ("\x1b", "\x07", "\x9b", "\r"))

    def test_a_run_that_outlasts_the_wait_is_cancelled(self, capsys) -> None:
        api = _Api([_run("RUNNING")])
        rc, out = _go(api, capsys, env={TIMEOUT_ENV: "30"})
        assert rc == 1
        assert api.writes() == ["run-now", "runs/cancel"]
        assert api.body("runs/cancel") == {"run_id": 77}
        assert "did not end within 30s and was cancelled" in out and URL in out
        assert TIMEOUT_ENV in out

    def test_ctrl_c_cancels_the_run_and_still_interrupts(self, capsys) -> None:
        api = _Api([_run("RUNNING")])
        with pytest.raises(KeyboardInterrupt):
            _go(api, capsys, time_=_Time(interrupt_after=2))
        assert api.writes() == ["run-now", "runs/cancel"]
        assert "cancelled run 77" in capsys.readouterr().out

    def test_replace_and_build_adds_full_refresh_to_the_jobs_own_dbt_build(self, capsys) -> None:
        api = _Api([_run("TERMINATED", code="SUCCESS")])
        rc, out = _go(api, capsys, mode="replace-and-build")
        assert rc == 0, out
        assert api.body("run-now")["dbt_commands"] == [
            "dbt deps",
            "dbt build --select orders --full-refresh",
        ]

    def test_replace_and_build_without_dbt_commands_starts_nothing(self, capsys) -> None:
        api = _Api([_run("TERMINATED", code="SUCCESS")], settings={"tasks": []})
        rc, out = _go(api, capsys, mode="replace-and-build")
        assert rc == 1 and api.writes() == []
        assert "has no dbt commands" in out

    @pytest.mark.parametrize(
        "legacy, code",
        [
            ({"life_cycle_state": "SKIPPED", "state_message": "another run is active"}, "SKIPPED"),
            ({"life_cycle_state": "INTERNAL_ERROR", "result_state": "FAILED"}, "FAILED"),
            ({"life_cycle_state": "TERMINATED", "result_state": "TIMEDOUT"}, "TIMEDOUT"),
        ],
    )
    def test_a_run_that_ends_any_other_way_fails(self, capsys, legacy, code) -> None:
        # The deprecated ``state`` is read when there is no ``status``.
        api = _Api([{"run_id": 77, "state": legacy}])
        rc, out = _go(api, capsys)
        assert rc == 1 and f"ended {code}" in out

    def test_the_deprecated_state_alone_can_succeed(self, capsys) -> None:
        legacy = {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}
        api = _Api([{"run_id": 77, "state": {"life_cycle_state": "RUNNING"}}, {"state": legacy}])
        assert _go(api, capsys)[0] == 0

    def test_a_run_now_that_fails_is_reported_without_a_traceback(
        self, capsys, monkeypatch
    ) -> None:
        monkeypatch.setenv("DATABRICKS_TOKEN", TOKEN)
        api = _Api([], fail={"run-now": PermissionError(f"PERMISSION_DENIED for {TOKEN}")})
        rc, out = _go(api, capsys)
        assert rc == 1
        assert "could not start a run of Databricks job 'fluid sales.orders transform' (5)" in out
        assert "PermissionError" in out and TOKEN not in out
        # Tried three times, always with the one token: Databricks starts one run at most.
        assert api.writes() == ["run-now"] * 3
        tokens = {b["idempotency_token"] for _m, e, _q, b in api.calls if e == "run-now"}
        assert len(tokens) == 1
        assert "if Databricks started it anyway, it is on the job's page:" in out

    def test_a_poll_that_fails_says_the_run_may_still_be_going(self, capsys) -> None:
        api = _Api([_run("RUNNING")])
        first = api.do

        def flaky(method: str, path: str, query: Any = None, body: Any = None, **kw: Any) -> Any:
            if path.endswith("runs/get") and any(e == "runs/get" for _m, e, _q, _b in api.calls):
                raise ConnectionError("connection reset")
            return first(method, path, query=query, body=body, **kw)

        api.do = flaky  # type: ignore[method-assign]
        rc, out = _go(api, capsys)
        assert rc == 1
        assert "could not read run 77 (ConnectionError: connection reset); trying again" in out
        assert "could not read run 77 5 times in a row" in out and "may still be going" in out
        # A run that cannot be seen is never cancelled: it may be well under way.
        assert api.writes() == ["run-now"]
        assert f"run page: {URL}" in out

    def test_one_failed_read_is_tried_again_and_the_run_goes_on(self, capsys) -> None:
        # A plain 500, 502 or 504 the SDK does not repeat: one says nothing about the run.
        api = _Api([_run("RUNNING"), _run("TERMINATED", code="SUCCESS")])
        first = api.do
        failed: list = []

        def once(method: str, path: str, query: Any = None, body: Any = None, **kw: Any) -> Any:
            if path.endswith("runs/get") and not failed:
                failed.append(1)
                raise RuntimeError("502 Bad Gateway")
            return first(method, path, query=query, body=body, **kw)

        api.do = once  # type: ignore[method-assign]
        rc, out = _go(api, capsys)
        assert rc == 0, out
        assert "trying again" in out and "succeeded" in out
        assert api.writes() == ["run-now"]

    def test_run_now_is_tried_again_with_the_same_token(self, capsys) -> None:
        api = _Api([_run("TERMINATED", code="SUCCESS")])
        first = api.do
        tries: list = []

        def slow(method: str, path: str, query: Any = None, body: Any = None, **kw: Any) -> Any:
            if path.endswith("run-now"):
                tries.append(body["idempotency_token"])
                if len(tries) < 3:
                    raise TimeoutError("read timed out")
            return first(method, path, query=query, body=body, **kw)

        api.do = slow  # type: ignore[method-assign]
        rc, out = _go(api, capsys)
        assert rc == 0, out
        assert len(tries) == 3 and len(set(tries)) == 1

    def test_ctrl_c_once_the_run_has_ended_cancels_nothing(self, capsys) -> None:
        api = _Api(
            [_run("TERMINATED", code="SUCCESS", tasks=[_task("SUCCESS")])],
            fail={"runs/get-output": KeyboardInterrupt()},
        )
        with pytest.raises(KeyboardInterrupt):
            _go(api, capsys)
        assert api.writes() == ["run-now"]

    def test_sigterm_while_waiting_cancels_the_run(self, capsys) -> None:
        import os
        import signal
        import threading

        if threading.current_thread() is not threading.main_thread():
            pytest.skip("a signal handler needs the main thread")
        before = signal.getsignal(signal.SIGTERM)
        api = _Api([_run("RUNNING")])

        class _Kill(_Time):
            def sleep(self, seconds: float) -> None:
                os.kill(os.getpid(), signal.SIGTERM)  # what a cancelled CI job sends
                super().sleep(seconds)

        with pytest.raises(KeyboardInterrupt):
            _go(api, capsys, time_=_Kill())
        assert api.writes() == ["run-now", "runs/cancel"]
        assert signal.getsignal(signal.SIGTERM) is before  # put back


# ── choosing what to run ────────────────────────────────────────────────────
class TestRunJobBuild:
    def test_a_dry_run_starts_nothing(self, capsys) -> None:
        def boom() -> Any:
            raise AssertionError("a dry run created a client")

        rc = run_job_build(CONTRACT, BUILD, {}, dry_run=True, client_factory=boom)
        out = " ".join(capsys.readouterr().out.split())
        assert rc == 0
        assert "[DRY RUN] Build 'transform' - would start a run of Databricks job" in out

    @pytest.mark.parametrize("refs, said", [([], "no Databricks job"), ([1, 2], "2 Databricks")])
    def test_a_build_needs_exactly_one_job_in_the_state(self, capsys, refs, said) -> None:
        def boom() -> Any:
            raise AssertionError("created a client")

        jobs = {"transform": [JobRef(n) for n in refs]}
        assert run_job_build(CONTRACT, BUILD, jobs, client_factory=boom) == 1
        err = " ".join(capsys.readouterr().err.split())
        assert f"holds {said}" in err
        assert "tagged fluid_product=sales.orders, fluid_build=transform" in err

    def test_without_the_sdk_it_says_which_extra_to_install(self, capsys) -> None:
        def missing() -> Any:
            raise ImportError("No module named 'databricks'")

        assert run_job_build(CONTRACT, BUILD, JOBS, client_factory=missing) == 1
        assert "pip install 'data-product-forge[databricks]'" in capsys.readouterr().err

    def test_a_client_that_cannot_be_made_is_one_line(self, capsys, monkeypatch) -> None:
        monkeypatch.setenv("DATABRICKS_TOKEN", TOKEN)

        def cannot_configure() -> Any:
            raise ValueError(f"cannot configure default credentials {TOKEN}")

        assert run_job_build(CONTRACT, BUILD, JOBS, client_factory=cannot_configure) == 1
        err = capsys.readouterr().err
        assert "could not create the Databricks client" in err and TOKEN not in err

    def test_started_builds_are_the_non_continuous_databricks_dbt_builds(self) -> None:
        def build(build_id: str, platform: str = "databricks", trigger: str = "") -> Dict:
            execution: Dict[str, Any] = {"runtime": {"platform": platform}}
            if trigger:
                execution["trigger"] = {"type": trigger}
            return {"id": build_id, "engine": "dbt", "execution": execution}

        contract = {
            "builds": [
                build("a"),
                build("b", trigger="schedule"),
                build("c", trigger="continuous"),
                build("d", platform="local"),
            ]
        }
        assert [b["id"] for b in started_builds(contract, None)] == ["a", "b"]
        assert [b["id"] for b in started_builds(contract, "")] == ["a", "b"]
        assert [b["id"] for b in started_builds(contract, "b")] == ["b"]
        assert started_builds(contract, "c") == []


# ── the state, the commands and the cap ─────────────────────────────────────
def test_jobs_are_found_in_the_state_by_their_tags() -> None:
    def job(job_id: Any, product: str, build: str, **extra: Any) -> Dict[str, Any]:
        values = {"id": job_id, "url": f"u/{job_id}", "tags": {}}
        if product:
            values["tags"]["fluid_product"] = product
        if build:
            values["tags"]["fluid_build"] = build
        return {"type": "databricks_job", "mode": "managed", "values": values, **extra}

    state = [
        job("11", "sales.orders", "transform"),
        job("12", "sales.other", "transform"),  # another product's job
        job("13", "sales.orders", ""),  # not one of ours
        job("x", "sales.orders", "bad-id"),
        job("14", "sales.orders", "nightly", mode="data"),
        {"type": "databricks_sql_table", "values": {"id": "main.sales.orders"}},
        job("15", "sales.orders", "twice"),
        job("16", "sales.orders", "twice"),
    ]
    assert jobs_in_state(state, "sales.orders") == {
        "transform": [JobRef(11, "u/11")],
        "twice": [JobRef(15, "u/15"), JobRef(16, "u/16")],
    }


def test_the_job_the_emitter_writes_is_the_one_the_state_reader_finds() -> None:
    from fluid_build.iac.providers import databricks as dbx
    from fluid_build.iac.providers import databricks_jobs
    from fluid_build.iac.source import contract_source

    contract = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "sales", "table": "orders"},
                },
                "contract": {"schema": [{"name": "id", "type": "bigint"}]},
            }
        ],
        "builds": [
            {
                "id": "transform",
                "engine": "dbt",
                "repository": "https://github.com/example-org/orders?tag=v1",
                "outputs": ["orders"],
                "properties": {"model": "orders"},
                "execution": {"runtime": {"platform": "databricks"}},
            }
        ],
    }
    with contract_source(None):
        compiled = dbx._compile(contract, {dbx.WAREHOUSE_ENV: "wh-1"})
    assert not compiled.refusals
    ((key, body),) = compiled.resources["databricks_job"].items()
    # What ``tofu show -json`` lists for it once applied: the id, and the tags as sent.
    state = [
        {
            "address": f"databricks_job.{key}",
            "type": "databricks_job",
            "mode": "managed",
            "values": {"id": "42", "tags": body["tags"]},
        }
    ]
    product = databricks_jobs.job_product(contract)
    assert job_run.jobs_in_state(state, product) == {"transform": [JobRef(42)]}


@pytest.mark.parametrize(
    "commands, expected",
    [
        (
            ["dbt deps", "dbt build --select orders+"],
            ["dbt deps", "dbt build --select orders+ --full-refresh"],
        ),
        (["dbt seed", "dbt run"], ["dbt seed --full-refresh", "dbt run --full-refresh"]),
        (["dbt build --full-refresh"], ["dbt build --full-refresh"]),
        (["dbt test"], ["dbt test"]),
    ],
)
def test_full_refresh_goes_on_the_commands_that_take_it(commands, expected) -> None:
    settings = {"tasks": [{"task_key": "x"}, {"dbt_task": {"commands": commands}}]}
    assert full_refresh_commands(settings) == expected


def test_full_refresh_needs_dbt_commands() -> None:
    assert full_refresh_commands({"tasks": [{"notebook_task": {}}]}) is None


@pytest.mark.parametrize(
    "settings, env, expected",
    [
        pytest.param({}, {}, DEFAULT_TIMEOUT_SECONDS, id="default-hour"),
        pytest.param({}, {TIMEOUT_ENV: "120"}, 120, id="env"),
        pytest.param({}, {TIMEOUT_ENV: "0"}, DEFAULT_TIMEOUT_SECONDS, id="env-zero-ignored"),
        pytest.param({}, {TIMEOUT_ENV: "2h"}, DEFAULT_TIMEOUT_SECONDS, id="env-not-a-number"),
        pytest.param(
            {"tasks": [{"timeout_seconds": 600}]}, {}, DEFAULT_TIMEOUT_SECONDS, id="short-job"
        ),
        pytest.param(
            {
                "tasks": [
                    {"timeout_seconds": 7200, "max_retries": 1, "min_retry_interval_millis": 60000}
                ]
            },
            {},
            7200 * 2 + 60 + 900,
            id="task-timeout-and-retries",
        ),
        pytest.param({"timeout_seconds": 5400}, {}, 5400 + 900, id="job-timeout"),
        pytest.param(
            {"tasks": [{"timeout_seconds": 7200}, {"task_key": "no-timeout"}]},
            {},
            DEFAULT_TIMEOUT_SECONDS,
            id="a-task-without-timeout",
        ),
        pytest.param(
            {"tasks": [{"timeout_seconds": 7200, "max_retries": -1}]},
            {},
            DEFAULT_TIMEOUT_SECONDS,
            id="endless-retries",
        ),
        pytest.param(
            {"timeout_seconds": 99999}, {TIMEOUT_ENV: "300"}, 300, id="env-wins-over-the-job"
        ),
    ],
)
def test_the_wait_cap(settings, env, expected) -> None:
    assert wait_budget(settings, env) == expected


def test_a_success_shows_dbt_s_own_summary(capsys) -> None:
    logs = (
        "\x1b[0m19:54:43  \x1b[32mCompleted successfully\x1b[0m\n"
        "\x1b[0m19:54:43  Done. PASS=1 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=1\n"
    )
    api = _Api(
        [_run("TERMINATED", code="SUCCESS", tasks=[_task("SUCCESS")])],
        outputs={78: {"logs": logs}},
    )
    rc, out = _go(api, capsys)
    assert rc == 0, out
    assert "task 'dbt': Done. PASS=1 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=1" in out


def test_a_summary_that_cannot_be_read_does_not_fail_the_build(capsys) -> None:
    api = _Api(
        [_run("TERMINATED", code="SUCCESS", tasks=[_task("SUCCESS")])],
        fail={"runs/get-output": ConnectionError("connection reset")},
    )
    rc, out = _go(api, capsys)
    assert rc == 0 and "task 'dbt'" not in out and "succeeded" in out


def test_the_task_error_is_one_line(capsys) -> None:
    # As Databricks words it for a dbt task: the shell command quoted with "\n" escapes.
    error = "Command 'dbt build --select orders\\nelse\\n  exit 1\\n\\nfi\\n' returned non-zero exit status 1."
    api = _Api(
        [_run("TERMINATED", code="RUN_EXECUTION_ERROR", tasks=[_task("RUN_EXECUTION_ERROR")])],
        outputs={78: {"error": error}},
    )
    rc, out = _go(api, capsys)
    assert rc == 1
    assert (
        "task 'dbt': Command 'dbt build --select orders else exit 1 fi ' returned non-zero" in out
    )


def test_a_run_id_from_the_list_is_printed_as_a_number_only(capsys) -> None:
    api = _Api(
        [_run("QUEUED"), _run("TERMINATED", code="SUCCESS")],
        active=[{"run_id": "\x1b[2J12"}, {"run_id": 77}],
    )
    rc, out = _go(api, capsys)
    assert rc == 0 and "\x1b" not in out
    assert "waiting for the workspace's limit" in out  # nothing but a number counts as a run


def test_a_run_that_cannot_be_cancelled_at_the_cap_is_said(capsys) -> None:
    api = _Api([_run("RUNNING")], fail={"runs/cancel": ConnectionError("down")})
    rc, out = _go(api, capsys, env={TIMEOUT_ENV: "30"})
    assert rc == 1
    assert "could not cancel run 77: ConnectionError: down" in out
    assert "did not end within 30s." in out and f"run page: {URL}" in out


@pytest.mark.parametrize("value", ["\u00b2", "9" * 5000])
def test_a_timeout_int_cannot_read_is_said_before_anything_starts(capsys, value) -> None:
    # str.isdigit() takes a superscript two and a 5000-digit number; int() refuses both.
    api = _Api([_run("TERMINATED", code="SUCCESS")])
    rc, out = _go(api, capsys, env={TIMEOUT_ENV: value})
    assert rc == 0, out
    assert "is not a whole number of seconds above 0" in out


def test_a_build_without_an_id_finds_the_job_the_emitter_tagged_by_position(capsys) -> None:
    build = {"engine": "dbt", "execution": {"runtime": {"platform": "databricks"}}}
    contract = {"id": "sales.orders", "builds": [build]}
    api = _Api([_run("TERMINATED", code="SUCCESS")])
    rc = run_job_build(
        contract,
        build,
        {"0": [JobRef(5)]},
        client_factory=lambda: _Client(api),
        env={},
        sleep=_Time().sleep,
        clock=_Time().clock,
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out
    assert "started run 77 of Databricks job 'fluid sales.orders 0' (5)" in out


def test_a_queued_run_of_the_job_is_not_the_one_ahead(capsys) -> None:
    api = _Api(
        [_run("QUEUED"), _run("TERMINATED", code="SUCCESS")],
        active=[{"run_id": 90, "status": {"state": "QUEUED"}}, {"run_id": 77}],
    )
    rc, out = _go(api, capsys)
    assert rc == 0
    assert "behind run 90" not in out and "waiting for the workspace's limit" in out


@pytest.mark.parametrize("value", ["1h", "0", "-5", "soon"])
def test_a_timeout_that_is_not_seconds_is_said(capsys, value) -> None:
    api = _Api([_run("TERMINATED", code="SUCCESS")])
    rc, out = _go(api, capsys, env={TIMEOUT_ENV: value})
    assert rc == 0
    assert f"{TIMEOUT_ENV}='{value}' is not a whole number of seconds above 0" in out
    assert "waiting up to 1h00m00s" in out


def test_a_timeout_in_seconds_is_taken_quietly(capsys) -> None:
    api = _Api([_run("TERMINATED", code="SUCCESS")])
    rc, out = _go(api, capsys, env={TIMEOUT_ENV: "120"})
    assert rc == 0 and TIMEOUT_ENV not in out


def test_a_sighup_the_operator_ignores_stays_ignored(capsys) -> None:
    """`nohup fluid apply ...` ignores SIGHUP: a closed terminal must not cancel the run."""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("a signal handler needs the main thread")
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    seen: list = []
    try:

        class _Look(_Time):
            def sleep(self, seconds: float) -> None:
                seen.append(signal.getsignal(signal.SIGHUP))
                super().sleep(seconds)

        api = _Api([_run("RUNNING"), _run("TERMINATED", code="SUCCESS")])
        assert _go(api, capsys, time_=_Look())[0] == 0
        assert seen and all(handler is signal.SIG_IGN for handler in seen)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)


# ── the contract's DQ rules, as dbt tests of the run ───────────────────────

DQ_BUILD: Dict[str, Any] = {
    "id": "transform",
    "engine": "dbt",
    "outputs": ["orders"],
    "execution": {"runtime": {"platform": "databricks"}},
}
DQ_CONTRACT: Dict[str, Any] = {
    "id": "sales.orders",
    "exposes": [
        {
            "exposeId": "orders",
            "binding": {"platform": "databricks"},
            "contract": {
                "schema": [{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}],
                "dq": {
                    "rules": [
                        {
                            "id": "id_unique",
                            "type": "uniqueness",
                            "selector": "id",
                            "severity": "critical",
                        },
                        {
                            "id": "status_set",
                            "type": "completeness",
                            "selector": "status",
                            "severity": "warn",
                        },
                    ]
                },
            },
        }
    ],
    "builds": [DQ_BUILD],
}
DQ_LOGS = (
    "\x1b[0m12:00:02  1 of 3 \x1b[32mPASS\x1b[0m id_unique ............ [\x1b[32mPASS\x1b[0m in 1.02s]\n"
    "\x1b[0m12:00:03  2 of 3 \x1b[33mWARN 4\x1b[0m status_set .......... [\x1b[33mWARN 4\x1b[0m in 0.50s]\n"
    "\x1b[0m12:00:04  Done. PASS=2 WARN=1 ERROR=0 SKIP=0 NO-OP=0 TOTAL=3\n"
)


def _go_dq(api: _Api, capsys: pytest.CaptureFixture) -> tuple:
    time_ = _Time()
    rc = run_job_build(
        DQ_CONTRACT,
        DQ_BUILD,
        JOBS,
        mode="amend-and-build",
        client_factory=lambda: _Client(api),
        env={},
        sleep=time_.sleep,
        clock=time_.clock,
    )
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def test_a_success_reports_each_contract_rule_dbt_tested(capsys) -> None:
    api = _Api(
        [_run("TERMINATED", code="SUCCESS", tasks=[_task("SUCCESS")])],
        outputs={78: {"logs": DQ_LOGS}},
    )
    rc, out = _go_dq(api, capsys)
    assert rc == 0, out
    assert "contract rules as dbt tests: 1 pass, 1 warn" in out
    assert "✓ id_unique: PASS - uniqueness id, severity error" in out
    assert "! status_set: WARN 4 rows - completeness status, severity warn" in out
    # One read of the task's output serves the summary and the rules.
    assert [c[1] for c in api.calls].count("runs/get-output") == 1


def test_a_failed_test_is_named_before_the_end_of_the_output(capsys) -> None:
    logs = DQ_LOGS.replace("PASS\x1b[0m id_unique", "FAIL 2\x1b[0m id_unique")
    api = _Api(
        [_run("TERMINATED", code="RUN_EXECUTION_ERROR", tasks=[_task("RUN_EXECUTION_ERROR")])],
        outputs={78: {"error": "dbt command failed", "logs": logs}},
    )
    rc, out = _go_dq(api, capsys)
    assert rc == 1
    flat = " ".join(out.split())
    assert "task 'dbt': dbt command failed" in flat
    assert "✗ id_unique: FAIL 2 rows - uniqueness id, severity error" in flat
    assert flat.index("id_unique: FAIL 2 rows") < flat.index("dbt output:")


def test_rules_are_reported_when_a_later_task_fails(capsys) -> None:
    keys = {**_task("RUN_EXECUTION_ERROR", run_id=79), "task_key": "fluid_keys"}
    api = _Api(
        [_run("TERMINATED", code="RUN_EXECUTION_ERROR", tasks=[_task("SUCCESS"), keys])],
        outputs={78: {"logs": DQ_LOGS}, 79: {"error": "key failed"}},
    )
    rc, out = _go_dq(api, capsys)
    assert rc == 1
    assert "✓ id_unique: PASS" in out and "task 'fluid_keys': key failed" in out


def test_a_contract_without_rules_reports_none(capsys) -> None:
    api = _Api(
        [_run("TERMINATED", code="SUCCESS", tasks=[_task("SUCCESS")])],
        outputs={78: {"logs": DQ_LOGS}},
    )
    rc, out = _go(api, capsys)
    assert rc == 0 and "contract rules" not in out
