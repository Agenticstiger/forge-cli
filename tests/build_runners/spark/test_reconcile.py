# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""reconcile / run_once / status against an in-memory adapter and a fake clock."""

from __future__ import annotations

import dataclasses

import pytest
from botocore.exceptions import ClientError

from fluid_build.build_runners.spark import _reconcile as rc
from fluid_build.build_runners.spark._adapters import RunInfo

ENV = {rc.WAIT_ENV: "5"}


def info(run_id, state, *, hash_=None, source=None, checkpoint=None, attempt=None, details=""):
    return RunInfo(
        run_id, state, state, config_hash=hash_, source_id=source, checkpoint_id=checkpoint,
        attempt=attempt, details=details,
    )  # fmt: skip


class FakeAdapter:
    """History is newest first; ``scripts`` maps a run id to the states its reads return
    (the last one repeats); ``start_states`` scripts the run ``start`` creates ("new")."""

    def __init__(self, history=(), start_states=("RUNNING",), start_details=""):
        self.history = list(history)
        self.scripts = {h.run_id: [h.state] for h in history}
        self.start_states = list(start_states)
        self.start_details = start_details
        self.calls = []

    def where(self):
        return "fake cluster"

    def runs(self):
        return list(self.history)

    def start(self, token):
        self.calls.append("start")
        self.scripts["new"] = list(self.start_states)
        return "new"

    def cancel(self, run_id):
        self.calls.append(f"cancel {run_id}")

    def get(self, run_id):
        states = self.scripts[run_id]
        state = states.pop(0) if len(states) > 1 else states[0]
        if state == "INTERRUPT":
            raise KeyboardInterrupt
        return info(run_id, state, details=self.start_details if run_id == "new" else "")


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def use(monkeypatch):
    """Make ``adapter_for`` return the fake (and count how often it is built)."""
    built = []

    def install(fake):
        monkeypatch.setattr(rc, "adapter_for", lambda run, clients=None: built.append(run) or fake)
        return built

    return install


def go(run, clock, **kw):
    return rc.reconcile([run], sleep=clock.sleep, now=clock.now, env=ENV, **kw)[0]


def test_no_runs_starts_one_and_polls_until_it_runs(run, clock, use):
    fake = FakeAdapter(start_states=("PENDING", "PENDING", "RUNNING"))
    use(fake)
    out = go(run, clock)
    assert (out.action, out.run_id, out.state, out.error) == ("started", "new", "RUNNING", False)
    assert fake.calls == ["start"]
    assert clock.slept == [1.0, 2.0]  # a second longer each poll


def test_the_same_config_running_is_left_alone(run, clock, use):
    fake = FakeAdapter([info("jr1", "RUNNING", hash_=run.config_hash)])
    use(fake)
    out = go(run, clock)
    assert (out.action, out.run_id, out.error, fake.calls) == ("unchanged", "jr1", False, [])


def test_a_running_job_on_attempt_three_is_unchanged_with_a_note(run, clock, use):
    use(FakeAdapter([info("jr1", "RUNNING", hash_=run.config_hash, attempt=3)]))
    out = go(run, clock)
    assert out.action == "unchanged" and "attempt 3" in out.message


def test_another_config_is_cancelled_waited_for_and_replaced(run, clock, use):
    fake = FakeAdapter(
        [info("old1", "RUNNING", hash_="stale")], start_states=("PENDING", "RUNNING")
    )
    fake.scripts["old1"] = ["CANCELLING", "CANCELLED"]
    use(fake)
    out = go(run, clock)
    assert (out.action, out.cancelled, out.state, out.error) == (
        "restarted",
        ["old1"],
        "RUNNING",
        False,
    )
    assert fake.calls == ["cancel old1", "start"]


def test_a_new_topic_on_the_same_checkpoint_is_refused_before_anything_is_cancelled(
    run, clock, use
):
    fake = FakeAdapter(
        [info("old1", "RUNNING", hash_="stale", source="other-topic", checkpoint=run.checkpoint_id)]
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.calls) == ("refused", True, [])
    assert "checkpoint" in out.message and "Nothing was cancelled" in out.message


def test_a_new_topic_on_a_new_checkpoint_restarts(run, clock, use):
    moved = dataclasses.replace(run, checkpoint_id="newcheckpoint0000", source_id="newsource")
    fake = FakeAdapter(
        [info("old1", "RUNNING", hash_="stale", source=run.source_id, checkpoint=run.checkpoint_id)]
    )
    fake.scripts["old1"] = ["CANCELLED"]
    use(fake)
    out = go(moved, clock)
    assert (out.action, out.error, out.cancelled) == ("restarted", False, ["old1"])


def test_a_failed_last_run_with_this_config_is_an_error_not_a_restart(run, clock, use):
    fake = FakeAdapter(
        [info("jr9", "FAILED", hash_=run.config_hash, attempt=4, details="OOM in driver")]
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.calls) == ("failed", True, [])
    assert "jr9" in out.message and "OOM in driver" in out.message and "--restart" in out.message


def test_restart_failed_starts_it_anyway(run, clock, use):
    fake = FakeAdapter([info("jr9", "FAILED", hash_=run.config_hash)])
    use(fake)
    out = go(run, clock, restart_failed=True)
    assert (out.action, out.error, fake.calls) == ("started", False, ["start"])


def test_a_failed_run_of_an_older_config_does_not_block_a_new_one(run, clock, use):
    fake = FakeAdapter([info("jr9", "FAILED", hash_="older")])
    use(fake)
    assert go(run, clock).action == "started"


def test_a_cancelled_run_that_never_ends_is_an_error_and_nothing_starts(run, clock, use):
    fake = FakeAdapter([info("old1", "RUNNING", hash_="stale")])
    fake.scripts["old1"] = ["CANCELLING"]
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.run_id) == ("failed", True, "old1")
    assert fake.calls == ["cancel old1"]
    assert rc.WAIT_ENV in out.message and sum(clock.slept) <= 5


def test_a_started_run_that_fails_at_once_is_an_error_naming_the_run(run, clock, use):
    fake = FakeAdapter(start_states=("FAILED",), start_details="bad conf")
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.run_id) == ("started", True, "new")
    assert "run new" in out.message and "bad conf" in out.message


def test_a_run_still_pending_after_the_budget_is_reported_not_failed(run, clock, use):
    use(FakeAdapter(start_states=("PENDING",)))
    out = go(run, clock)
    assert (out.state, out.error) == ("PENDING", False) and "still starting" in out.message


def test_dry_run_changes_nothing(run, clock, use):
    fake = FakeAdapter([info("old1", "RUNNING", hash_="stale")])
    use(fake)
    out = go(run, clock, dry_run=True)
    assert (out.action, fake.calls) == ("planned", []) and "old1" in out.message


def test_a_scheduled_build_is_reported_and_never_touches_aws(run, clock, use):
    built = use(FakeAdapter())
    scheduled = dataclasses.replace(run, scheduled=True, mode="batch")
    out = go(scheduled, clock)
    assert (out.action, out.error, built) == ("scheduled", False, [])
    assert "fluid spark run" in out.message


def test_an_aws_error_is_that_builds_outcome_and_the_others_still_reconcile(
    run, clock, monkeypatch
):
    second = dataclasses.replace(run, build="second", run_name="fluid-second")
    good = FakeAdapter()

    def build(r, clients=None):
        if r.build == "ingest-orders":
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListJobRuns"
            )
        return good

    monkeypatch.setattr(rc, "adapter_for", build)
    first, other = rc.reconcile([run, second], sleep=clock.sleep, now=clock.now, env=ENV)
    assert first.error and first.action == "failed" and "AccessDeniedException" in first.message
    assert (other.action, other.error) == ("started", False)


def test_wait_budget_reads_the_env_and_ignores_garbage():
    assert rc.wait_budget({rc.WAIT_ENV: "7"}) == 7
    assert rc.wait_budget({rc.WAIT_ENV: "x"}) == rc.DEFAULT_WAIT_S
    assert rc.wait_budget({rc.WAIT_ENV: "-1"}) == rc.DEFAULT_WAIT_S
    assert rc.wait_budget({}) == rc.DEFAULT_WAIT_S


# --- run_once -------------------------------------------------------------------------


@pytest.fixture
def scheduled(run):
    return dataclasses.replace(run, scheduled=True, mode="batch")


def once(run, clock, say=None, **kw):
    return rc.run_once(
        run, sleep=clock.sleep, now=clock.now, env=ENV, say=say or (lambda _t: None), **kw
    )


def test_a_scheduled_run_waits_to_succeeded(scheduled, clock, use):
    fake = FakeAdapter(start_states=("PENDING", "RUNNING", "SUCCEEDED"))
    use(fake)
    said = []
    out = once(scheduled, clock, say=said.append)
    assert (out.action, out.error, out.state, out.run_id) == (
        "succeeded",
        False,
        "SUCCEEDED",
        "new",
    )
    assert fake.calls == ["start"] and "started run new" in said[0]


def test_a_scheduled_run_that_fails_is_an_error(scheduled, clock, use):
    use(FakeAdapter(start_states=("RUNNING", "FAILED"), start_details="exit 1"))
    out = once(scheduled, clock)
    assert (out.action, out.error) == ("failed", True) and "exit 1" in out.message


def test_no_wait_returns_once_started(scheduled, clock, use):
    fake = FakeAdapter(start_states=("PENDING",))
    use(fake)
    out = once(scheduled, clock, wait=False)
    assert (out.action, out.state, out.error, clock.slept) == ("started", "PENDING", False, [])


def test_a_run_that_outlives_the_timeout_is_cancelled(scheduled, clock, use):
    fake = FakeAdapter(start_states=("RUNNING",))
    use(fake)
    out = once(scheduled, clock, timeout=5)
    assert out.error and "cancelled" in out.message
    assert fake.calls == ["start", "cancel new"]


def test_ctrl_c_while_waiting_cancels_the_run_and_is_a_cancelled_outcome(scheduled, clock, use):
    fake = FakeAdapter(start_states=("RUNNING", "INTERRUPT"))
    use(fake)
    said = []
    out = once(scheduled, clock, say=said.append)
    assert (out.action, out.error, out.run_id) == ("cancelled", True, "new")
    assert fake.calls == ["start", "cancel new"]
    assert any("cancelled run new" in line for line in said)


def test_a_streaming_build_is_reconciled_instead(run, clock, use):
    fake = FakeAdapter()
    use(fake)
    assert once(run, clock).action == "started"
    fake = FakeAdapter([info("jr9", "FAILED", hash_=run.config_hash)])
    use(fake)
    assert once(run, clock).action == "failed"
    assert once(run, clock, restart=True).action == "started"


def test_run_once_turns_an_aws_error_into_an_outcome(scheduled, clock, monkeypatch):
    def boom(run, clients=None):
        raise rc.SparkRunError("no application")

    monkeypatch.setattr(rc, "adapter_for", boom)
    out = once(scheduled, clock)
    assert (out.action, out.error, out.message) == ("failed", True, "no application")


# --- status ---------------------------------------------------------------------------


def test_status_flags_the_runs_that_match_the_contract(run, use):
    use(
        FakeAdapter([info("a", "RUNNING", hash_=run.config_hash), info("b", "FAILED", hash_="old")])
    )
    (row,) = rc.status([run])
    assert row["where"] == "fake cluster" and row["configHash"] == run.config_hash
    assert [(r["runId"], r["current"]) for r in row["runs"]] == [("a", True), ("b", False)]


def test_status_limits_runs_and_reports_an_error_as_the_row(run, monkeypatch):
    monkeypatch.setattr(
        rc,
        "adapter_for",
        lambda r, c=None: FakeAdapter([info(str(i), "RUNNING") for i in range(9)]),
    )
    assert len(rc.status([run], limit=2)[0]["runs"]) == 2

    def boom(r, c=None):
        raise rc.SparkRunError("nope")

    monkeypatch.setattr(rc, "adapter_for", boom)
    assert rc.status([run])[0]["error"] == "nope"


# --- redaction ------------------------------------------------------------------------

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # pragma: allowlist secret


def test_a_failure_reason_with_a_secret_is_redacted_in_the_outcome(run, clock, use):
    use(
        FakeAdapter(
            [
                info(
                    "jr9",
                    "FAILED",
                    hash_=run.config_hash,
                    details=f"aws_secret_access_key={SECRET}",
                )
            ]
        )
    )
    out = go(run, clock)
    assert (
        SECRET not in out.message and SECRET not in str(out.as_dict()) and SECRET not in out.line()
    )


def test_the_plan_action_never_carries_the_mask_key(run):
    from tests.spark.conftest import MASK_KEY_ARN

    assert MASK_KEY_ARN not in str(run.plan_action(1)) and MASK_KEY_ARN not in str(run.tags())
