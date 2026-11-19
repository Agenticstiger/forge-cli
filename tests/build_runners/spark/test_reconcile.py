# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""reconcile / run_once / run_build against an in-memory adapter and a fake clock."""

from __future__ import annotations

import dataclasses

import pytest

# botocore comes with boto3, the [aws] extra; the reconcile uses its ClientError shape.
ClientError = pytest.importorskip("botocore.exceptions").ClientError

from fluid_build.build_runners.spark import _reconcile as rc
from fluid_build.build_runners.spark._adapters import RunInfo, SparkRunError

ENV = {rc.WAIT_ENV: "5"}


#: The wall clock of a reconcile (epoch seconds): a run ``created`` at 0 is days old.
WALL = 1_000_000.0


def info(
    run_id,
    state,
    *,
    hash_=None,
    source=None,
    checkpoint=None,
    attempt=None,
    details="",
    created=0.0,
    restart_of=None,
    raw=None,
    lost=False,
):
    return RunInfo(
        run_id, state, raw or state, config_hash=hash_, source_id=source,
        checkpoint_id=checkpoint, attempt=attempt, details=details, created=created,
        restart_of=restart_of, lost=lost,
    )  # fmt: skip


class FakeAdapter:
    """History is newest first; ``scripts`` maps a run id to the states its reads return
    (the last one repeats); ``start_states`` scripts the run ``start`` creates ("new").
    ``later`` is the history ``runs`` returns after a start; ``job`` the job config the last
    apply uploaded (an AWS runtime's), ``target_error`` what ``check_target`` raises."""

    uploads_job = False

    def __init__(
        self, history=(), start_states=("RUNNING",), start_details="", *, later=None, job=None,
        uploads=None, target_error=None,
    ):  # fmt: skip
        self.history = list(history)
        self.later = later
        self.scripts = {h.run_id: [h.state] for h in [*history, *(later or ())]}
        self.start_states = list(start_states)
        self.start_details = start_details
        self.job = job
        self.uploads_job = job is not None if uploads is None else uploads
        self.target_error = target_error
        self.calls = []
        self.restart_of = []
        self.since = []

    def where(self):
        return "fake cluster"

    def runs(self, since=None):
        self.since.append(since)
        if self.later is not None and "start" in self.calls:
            return list(self.later)
        return list(self.history)

    def start(self, token, restart_of=None):
        self.calls.append("start")
        self.restart_of.append(restart_of)
        self.scripts["new"] = list(self.start_states)
        return "new"

    def check_target(self):
        if self.target_error:
            raise SparkRunError(self.target_error)

    def deployed_job(self):
        return self.job.encode("utf-8") if isinstance(self.job, str) else self.job

    def cancel(self, run_id):
        self.calls.append(f"cancel {run_id}")

    def get(self, run_id):
        states = self.scripts[run_id]
        state = states.pop(0) if len(states) > 1 else states[0]
        if state == "INTERRUPT":
            raise KeyboardInterrupt
        return info(run_id, state, details=self.start_details if run_id == "new" else "")


def go(run, clock, **kw):
    kw.setdefault("wall", lambda: WALL)
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
    assert "jr9" in out.message and "OOM in driver" in out.message
    assert "fix the cause and change the build" in out.message
    assert "EMR Serverless retries a failed stream (retryPolicy.maxFailedAttemptsPerHour)" in (
        out.message
    )


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
    scheduled = dataclasses.replace(
        run, scheduled=True, mode="batch", checkpoint=None, checkpoint_id=None, source_id=None
    )
    out = go(scheduled, clock)
    assert (out.action, out.error, built) == ("scheduled", False, [])
    assert "fluid apply <contract> --mode amend-and-build --build-id" in out.message


def test_an_aws_error_is_that_builds_outcome_and_the_others_still_reconcile(
    run, clock, monkeypatch
):
    second = dataclasses.replace(run, build="second", run_name="fluid-second")
    good = FakeAdapter()
    good.run = second

    def build(r, clients=None, local_host=None):
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
    return dataclasses.replace(
        run, scheduled=True, mode="batch", checkpoint=None, checkpoint_id=None, source_id=None
    )


def once(run, clock, say=None, **kw):
    return rc.run_once(run, sleep=clock.sleep, now=clock.now, say=say or (lambda _t: None), **kw)


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


def test_a_run_that_outlives_the_timeout_is_cancelled(scheduled, clock, use):
    fake = FakeAdapter(start_states=("RUNNING",))
    use(fake)
    out = once(scheduled, clock, timeout=5)
    assert out.error and "it was cancelled" in out.message and out.cancelled == ["new"]
    assert fake.calls == ["start", "cancel new"]


def test_ctrl_c_while_waiting_cancels_the_run_and_is_a_cancelled_outcome(scheduled, clock, use):
    fake = FakeAdapter(start_states=("RUNNING", "INTERRUPT"))
    use(fake)
    said = []
    out = once(scheduled, clock, say=said.append)
    assert (out.action, out.error, out.run_id) == ("cancelled", True, "new")
    assert out.cancelled == ["new"] and "was cancelled" in out.message
    assert fake.calls == ["start", "cancel new"]
    assert any("cancelled run new" in line for line in said)


def test_a_timed_out_run_whose_cancel_fails_says_it_may_still_be_going(scheduled, clock, use):
    class Stuck(FakeAdapter):
        def cancel(self, run_id):
            raise RuntimeError("AccessDeniedException")

    use(Stuck(start_states=("RUNNING",)))
    out = once(scheduled, clock, timeout=5, say=lambda _line: None)
    assert (out.action, out.error, out.cancelled) == ("failed", True, [])
    assert "could not be cancelled" in out.message and "it was cancelled" not in out.message


def test_ctrl_c_whose_cancel_fails_says_the_run_may_still_be_going(scheduled, clock, use):
    class Stuck(FakeAdapter):
        def cancel(self, run_id):
            raise RuntimeError("AccessDeniedException")

    use(Stuck(start_states=("RUNNING", "INTERRUPT")))
    out = once(scheduled, clock, say=lambda _line: None)
    assert (out.action, out.error, out.run_id, out.cancelled) == ("cancelled", True, "new", [])
    assert "could not be cancelled" in out.message and "by hand" in out.message


def test_run_once_has_no_run_to_start_for_a_stream(run, clock, use):
    built = use(FakeAdapter())
    with pytest.raises(ValueError, match="stream that fluid apply keeps running"):
        once(run, clock)
    assert built == []


def test_run_once_turns_an_aws_error_into_an_outcome(scheduled, clock, monkeypatch):
    def boom(run, clients=None, local_host=None):
        raise rc.SparkRunError("no application")

    monkeypatch.setattr(rc, "adapter_for", boom)
    out = once(scheduled, clock)
    assert (out.action, out.error, out.message) == ("failed", True, "no application")


# --- run_build: fluid apply --mode amend-and-build ------------------------------------


def _available_now(contract, **runtime):
    """The conftest contract's build as a stream that runs to completion."""
    contract["builds"][0]["properties"]["stream"]["trigger"] = {"availableNow": True}
    if runtime:
        contract["builds"][0]["execution"] = {"runtime": runtime}
    return contract, contract["builds"][0]


def test_run_build_starts_a_scheduled_build_and_waits_for_it(contract, use):
    contract, build = _available_now(contract)
    fake = FakeAdapter(start_states=("SUCCEEDED",))
    use(fake)
    said = []
    assert rc.run_build(contract, build, dry_run=False, say=said.append) == 0
    assert fake.calls == ["start"]
    assert said[-1].startswith("Spark ingest-orders (emr-serverless): succeeded run new")


def test_run_build_fails_when_the_run_does(contract, use):
    contract, build = _available_now(contract)
    use(FakeAdapter(start_states=("FAILED",), start_details="exit 1"))
    said = []
    assert rc.run_build(contract, build, dry_run=False, say=said.append) == 1
    assert "exit 1" in said[-1]


@pytest.mark.parametrize(
    "runtime, wait", [({}, rc.DEFAULT_RUN_WAIT_S), ({"timeout": "PT2H"}, 7200)]
)
def test_run_build_waits_as_long_as_the_build_says(contract, monkeypatch, runtime, wait):
    contract, build = _available_now(contract, **runtime)
    seen = {}

    def run_once(run, **kw):
        seen.update(kw, build=run.build)
        return rc.Outcome(run.build, run.runtime, "succeeded")

    monkeypatch.setattr(rc, "run_once", run_once)
    assert rc.run_build(contract, build, dry_run=False, say=lambda _t: None) == 0
    assert (seen["build"], seen["timeout"]) == ("ingest-orders", wait)


def test_a_stream_fluid_apply_keeps_running_has_no_run_to_start(contract, use):
    built = use(FakeAdapter())
    assert rc.run_build(contract, contract["builds"][0], dry_run=False, say=print) is None
    assert built == []


def test_a_dry_run_says_what_it_would_start_and_reads_nothing(contract, use):
    contract, build = _available_now(contract)
    built = use(FakeAdapter())
    said = []
    assert rc.run_build(contract, build, dry_run=True, say=said.append) == 0
    assert built == []
    assert said == [
        "Spark ingest-orders (emr-serverless): would start one run and wait up to 21600 s "
        "for it to end"
    ]


def test_a_contract_before_the_managed_lane_is_refused(contract, use):
    contract, build = _available_now(contract)
    contract["fluidVersion"] = "0.7.6"
    built = use(FakeAdapter(start_states=("SUCCEEDED",)))
    with pytest.raises(ValueError, match="fluidVersion 0.7.7"):
        rc.run_build(contract, build, dry_run=False, say=print)
    assert built == []


def test_an_unusable_runtime_timeout_is_refused_before_anything_starts(contract, use):
    contract, build = _available_now(contract, timeout="soon")
    built = use(FakeAdapter(start_states=("SUCCEEDED",)))
    with pytest.raises(ValueError, match="execution.runtime.timeout 'soon'"):
        rc.run_build(contract, build, dry_run=False, say=print)
    assert built == []


def test_an_interrupted_run_is_cancelled_and_stops_the_apply(contract, use):
    contract, build = _available_now(contract)
    fake = FakeAdapter(start_states=("INTERRUPT",))
    use(fake)
    said = []
    with pytest.raises(KeyboardInterrupt):
        rc.run_build(contract, build, dry_run=False, say=said.append)
    assert fake.calls == ["start", "cancel new"]
    assert said[-1].startswith("Spark ingest-orders (emr-serverless): cancelled run new")


def test_run_refusal_reads_no_cloud_and_names_what_blocks_a_run(contract):
    contract, build = _available_now(contract)
    assert rc.run_refusal(contract, build) is None
    build["execution"] = {"runtime": {"timeout": "soon"}}
    assert "execution.runtime.timeout 'soon'" in rc.run_refusal(contract, build)
    # A stream fluid apply keeps running waits for nothing, so its timeout is not read.
    del build["properties"]["stream"]["trigger"]
    assert rc.run_refusal(contract, build) is None
    contract["fluidVersion"] = "0.7.6"
    assert "fluidVersion 0.7.7" in rc.run_refusal(contract, build)


def test_the_operators_policy_refuses_before_anything_starts(contract, use):
    from fluid_build._errors import FluidUserError

    contract, build = _available_now(contract)
    build["properties"]["parameters"]["spark"]["sparkConf"] = {"spark.jars": "s3://x/y.jar"}
    built = use(FakeAdapter(start_states=("SUCCEEDED",)))
    with pytest.raises(FluidUserError):
        rc.run_build(contract, build, dry_run=False, say=print)
    assert built == []


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


def test_a_new_source_on_a_checkpoint_an_older_run_used_is_refused(run, clock, use):
    # Newest run B read another checkpoint; A, older, read this one with another stream.
    b = info("B", "RUNNING", hash_="old", source="s2", checkpoint="other")
    a = info("A", "CANCELLED", hash_="older", source="s1", checkpoint=run.checkpoint_id)
    fake = FakeAdapter([b, a])
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.calls) == ("refused", True, [])


def test_a_second_copy_of_the_current_config_is_cancelled_and_the_oldest_kept(run, clock, use):
    """Whichever reconcile looks, the same copy stays: the oldest, which has run longest."""
    fake = FakeAdapter(
        [
            info("a2", "RUNNING", hash_=run.config_hash, created=20.0),
            info("a1", "RUNNING", hash_=run.config_hash, created=10.0),
        ]
    )
    fake.scripts["a2"] = ["RUNNING", "CANCELLED"]
    use(fake)
    out = go(run, clock)
    assert (out.run_id, out.cancelled, out.error) == ("a1", ["a2"], False)
    assert "start" not in fake.calls


def test_a_stream_moved_to_available_now_cancels_its_continuous_job(run, clock, use):
    fake = FakeAdapter([info("old", "RUNNING", hash_="continuous")])
    fake.scripts["old"] = ["RUNNING", "CANCELLED"]
    use(fake)
    out = go(dataclasses.replace(run, scheduled=True), clock)
    assert (out.action, out.cancelled, out.error) == ("scheduled", ["old"], False)
    assert "start" not in fake.calls


def test_a_retried_task_adopts_its_going_run_instead_of_starting_another(scheduled, clock, use):
    fake = FakeAdapter([info("going", "RUNNING", hash_=scheduled.config_hash)])
    fake.scripts["going"] = ["RUNNING", "SUCCEEDED"]
    use(fake)
    out = once(scheduled, clock)
    assert (out.action, out.run_id) == ("succeeded", "going")
    assert "start" not in fake.calls


def test_a_throttled_read_is_retried_and_five_in_a_row_leave_the_run_going(scheduled, clock, use):
    class Flaky(FakeAdapter):
        def __init__(self, fail):
            super().__init__(start_states=("RUNNING", "SUCCEEDED"))
            self.fail, self.reads = fail, 0

        def get(self, run_id):
            self.reads += 1
            if 1 < self.reads <= 1 + self.fail:
                raise ClientError({"Error": {"Code": "ThrottlingException"}}, "GetJobRun")
            return super().get(run_id)

    use(Flaky(fail=2))
    assert once(scheduled, clock).action == "succeeded"
    fake = Flaky(fail=5)
    use(fake)
    out = once(scheduled, clock)
    assert (out.error, out.run_id) == (True, "new") and "left going" in out.message
    assert not any(c.startswith("cancel") for c in fake.calls)


def test_ctrl_c_during_the_start_is_a_cancelled_outcome(scheduled, clock, use):
    class Interrupted(FakeAdapter):
        def start(self, token):
            raise KeyboardInterrupt

    use(Interrupted())
    out = once(scheduled, clock)
    assert (out.action, out.error, out.run_id) == ("cancelled", True, None)
